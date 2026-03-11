from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import requests

try:
    from SmartApi import SmartConnect
    import pyotp

    ANGEL_AVAILABLE = True
except Exception:
    ANGEL_AVAILABLE = False
    SmartConnect = None
    pyotp = None

from nifty_algo.brokers.base import BrokerInterface
from nifty_algo.config import config

logger = logging.getLogger(__name__)


class AngelBroker(BrokerInterface):
    """Angel Smart API broker implementation"""

    def __init__(
        self,
        api_key: str,
        client_code: str,
        mpin: str,
        totp_secret: str,
        log_dir: str = "algo_logs",
        load_contract_master: bool = True,
        master_cache_max_age_hours: int = 24,
    ):
        self.api_key = api_key
        self.client_code = client_code
        self.mpin = mpin
        self.totp_secret = totp_secret

        self.smart_api = None
        self.feed_token = None
        self.jwt_token = None

        self.symbol_token_cache = {}
        self.failed_lookups = set()
        self.last_api_call_time = 0
        self.api_call_delay = 0.1

        self.contract_master = None
        self.master_file_url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        self.master_cache_file = Path(log_dir) / "angel_contract_master.json"
        self.master_cache_max_age_hours = master_cache_max_age_hours
        self.load_contract_master = load_contract_master

    def initialize(self):
        """Initialize Angel Smart API connection"""
        if not ANGEL_AVAILABLE:
            raise ImportError("smartapi-python package not installed. Install with: pip install smartapi-python")

        if not all([self.api_key, self.client_code, self.mpin, self.totp_secret]):
            raise ValueError(
                "ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN (or ANGEL_PASSWORD), and ANGEL_TOTP_SECRET must be set"
            )

        self.smart_api = SmartConnect(api_key=self.api_key)

        totp_code = pyotp.TOTP(self.totp_secret).now()
        data = self.smart_api.generateSession(
            clientCode=self.client_code,
            password=self.mpin,
            totp=totp_code,
        )

        if data.get("status") and data.get("data"):
            self.jwt_token = data["data"]["jwtToken"]
            self.feed_token = self.smart_api.getfeedToken()

            if self.load_contract_master:
                self._load_contract_master()
            else:
                logger.info("Contract master loading disabled.")
            return True

        error_msg = data.get("message", "Unknown error")
        raise Exception(f"Angel API Login Failed: {error_msg}")

    def _load_contract_master(self):
        """Load Angel contract master file for token lookup (with local caching)"""
        try:
            cache_valid = False
            if self.master_cache_file.exists():
                cache_age = time.time() - self.master_cache_file.stat().st_mtime
                cache_age_hours = cache_age / 3600
                if cache_age_hours < self.master_cache_max_age_hours:
                    cache_valid = True
                    logger.info(f"Using cached contract master (age: {cache_age_hours:.1f} hours)")

            if cache_valid:
                try:
                    with open(self.master_cache_file, "r", encoding="utf-8") as f:
                        self.contract_master = json.load(f)
                    logger.info(f"Loaded {len(self.contract_master)} contracts from cache")
                    return
                except Exception as cache_error:
                    logger.warning(f"Failed to load cache: {cache_error}, downloading fresh...")

            logger.info("Downloading Angel contract master file...")
            response = requests.get(self.master_file_url, timeout=30)
            response.raise_for_status()
            self.contract_master = response.json()
            logger.info(f"Downloaded {len(self.contract_master)} contracts from server")

            try:
                self.master_cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.master_cache_file, "w", encoding="utf-8") as f:
                    json.dump(self.contract_master, f)
                logger.info(f"Cached contract master file to: {self.master_cache_file}")
            except Exception as save_error:
                logger.warning(f"Failed to save cache: {save_error}")

        except Exception as e:
            logger.warning(f"Failed to load contract master file: {e}")
            logger.warning("Will fall back to searchScrip API for token lookup")
            self.contract_master = None

    def get_nearest_expiry(self) -> Optional[date]:
        """
        Get the nearest NIFTY option expiry from the contract master.
        Reflects exchange calendar (no hardcoded weekday; holidays etc. are
        as per listed expiries). Returns date or None if master not loaded.
        """
        if not self.contract_master:
            return None
        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        expiry_re = re.compile(
            r"NIFTY(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})\d+(?:CE|PE)$"
        )
        today = date.today()
        expiries = set()
        try:
            for row in self.contract_master:
                if row.get("name") != "NIFTY" or row.get("instrumenttype") != "OPTIDX":
                    continue
                sym = row.get("symbol", "")
                m = expiry_re.match(sym)
                if not m:
                    continue
                dd, mon, yy = m.groups()
                year = 2000 + int(yy) if int(yy) < 50 else 1900 + int(yy)
                month = month_map.get(mon)
                if not month:
                    continue
                try:
                    d = date(year, month, int(dd))
                    if d >= today:
                        expiries.add(d)
                except (ValueError, TypeError):
                    continue
            if expiries:
                nearest = min(expiries)
                logger.info(
                    "Nearest expiry from contract master: %s",
                    nearest.strftime("%d-%b-%Y"),
                )
                return nearest
        except Exception as e:
            logger.warning("Failed to get expiry from contract master: %s", e)
        return None

    def _get_token_from_master(self, tradingsymbol):
        """
        Get option token from contract master file
        Returns: (token, symbol) or (None, None)
        """
        if not self.contract_master:
            return None, None

        try:
            for row in self.contract_master:
                if (
                    row.get("name") == "NIFTY"
                    and row.get("instrumenttype") == "OPTIDX"
                    and row.get("symbol") == tradingsymbol
                ):
                    token = row.get("token")
                    found_symbol = row.get("symbol")
                    if token:
                        return str(token), found_symbol

            return None, None

        except Exception as e:
            logger.error(f"Error parsing symbol from master file: {e}")
            return None, None

    def get_profile(self):
        """Get user profile"""
        try:
            return self.smart_api.getProfile(self.jwt_token)
        except Exception as e:
            return {"status": False, "message": str(e)}

    def get_spot_price(self, symbol):
        """Get Nifty spot price"""
        try:
            if "NIFTY50" in symbol or "NIFTY" in symbol:
                angel_symbol = "NSE_INDEX|Nifty 50"
            else:
                angel_symbol = symbol.replace(":", "_")

            ltp_data = self.smart_api.ltpData(
                exchange="NSE",
                tradingsymbol=angel_symbol.split("|")[-1],
                symboltoken=self._get_symbol_token(angel_symbol),
            )

            if ltp_data.get("status") and ltp_data.get("data"):
                return float(ltp_data["data"]["ltp"])
            return None
        except Exception as e:
            error_msg = str(e).lower()
            if "access denied" in error_msg or "exceeding access rate" in error_msg or "rate" in error_msg:
                delay = config.ANGEL_SPOT_RETRY_DELAY
                logger.warning(
                    "Spot price rate limited. Backing off for %ss and retrying once.",
                    delay,
                )
                time.sleep(delay)
                try:
                    ltp_data = self.smart_api.ltpData(
                        exchange="NSE",
                        tradingsymbol=angel_symbol.split("|")[-1],
                        symboltoken=self._get_symbol_token(angel_symbol),
                    )
                    if ltp_data.get("status") and ltp_data.get("data"):
                        return float(ltp_data["data"]["ltp"])
                except Exception as retry_exc:
                    logger.error(f"Error fetching spot price after retry: {retry_exc}")
                if config.NIFTY_PREOPEN_PRICE is not None:
                    logger.warning(
                        "Using NIFTY_PREOPEN_PRICE fallback for spot: %s",
                        config.NIFTY_PREOPEN_PRICE,
                    )
                    return float(config.NIFTY_PREOPEN_PRICE)
            else:
                logger.error(f"Error fetching spot price: {e}")
            return None

    def _get_symbol_token(self, symbol):
        """Get symbol token for Angel API (simplified - may need master file lookup)"""
        if "Nifty 50" in symbol or "NIFTY50" in symbol:
            return "99926000"
        return None

    def _get_exchange_for_symbol(self, symbol):
        if "CE" in symbol or "PE" in symbol:
            return "NFO"
        return "NSE"

    def get_option_quote(self, symbol):
        """Get option quote using Angel Smart API"""
        try:
            if ":" in symbol:
                parts = symbol.split(":")
            elif "|" in symbol:
                parts = symbol.split("|")
            else:
                parts = ["NSE", symbol]

            exchange = self._get_exchange_for_symbol(symbol)
            tradingsymbol = parts[1]

            try:
                time_since_last_call = time.time() - self.last_api_call_time
                if time_since_last_call < self.api_call_delay:
                    time.sleep(self.api_call_delay - time_since_last_call)

                market_data = self.smart_api.marketDataFull(
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                )
                self.last_api_call_time = time.time()

                if market_data.get("status") and market_data.get("data"):
                    data = market_data["data"]
                    return {
                        "ltp": float(data.get("ltp", 0)),
                        "open": float(data.get("open", 0)),
                        "high": float(data.get("high", 0)),
                        "low": float(data.get("low", 0)),
                        "close": float(data.get("close", 0)),
                        "volume": int(data.get("volume", 0)),
                        "previousClose": float(data.get("previousClose", 0)),
                    }
            except Exception as market_error:
                error_msg = str(market_error).lower()
                if "rate" in error_msg or "access denied" in error_msg or "exceeding access rate" in error_msg:
                    time.sleep(2)
                    try:
                        market_data = self.smart_api.marketDataFull(
                            exchange=exchange,
                            tradingsymbol=tradingsymbol,
                        )
                        self.last_api_call_time = time.time()
                        if market_data.get("status") and market_data.get("data"):
                            data = market_data["data"]
                            return {
                                "ltp": float(data.get("ltp", 0)),
                                "open": float(data.get("open", 0)),
                                "high": float(data.get("high", 0)),
                                "low": float(data.get("low", 0)),
                                "close": float(data.get("close", 0)),
                                "volume": int(data.get("volume", 0)),
                                "previousClose": float(data.get("previousClose", 0)),
                            }
                    except Exception:
                        pass

            symbol_token = self._get_option_token(symbol, exchange, tradingsymbol)
            if symbol_token:
                try:
                    time_since_last_call = time.time() - self.last_api_call_time
                    if time_since_last_call < self.api_call_delay:
                        time.sleep(self.api_call_delay - time_since_last_call)

                    ltp_data = self.smart_api.ltpData(
                        exchange=exchange,
                        tradingsymbol=tradingsymbol,
                        symboltoken=symbol_token,
                    )
                    self.last_api_call_time = time.time()

                    if ltp_data.get("status") and ltp_data.get("data"):
                        data = ltp_data["data"]
                        return {
                            "ltp": float(data.get("ltp", 0)),
                            "open": 0,
                            "high": 0,
                            "low": 0,
                            "close": float(data.get("ltp", 0)),
                            "volume": 0,
                            "previousClose": float(data.get("previousClose", 0)),
                        }
                except Exception:
                    pass

            return None
        except Exception as e:
            logger.error(f"Error fetching option quote for {symbol}: {e}")
            return None

    def _get_option_token(self, symbol, exchange=None, tradingsymbol=None):
        """Get option token using contract master file or searchScrip API"""
        if symbol in self.symbol_token_cache:
            return self.symbol_token_cache[symbol]

        if symbol in self.failed_lookups:
            return None

        try:
            if not tradingsymbol:
                if ":" in symbol:
                    parts = symbol.split(":")
                elif "|" in symbol:
                    parts = symbol.split("|")
                else:
                    parts = ["NSE", symbol]
                tradingsymbol = parts[1]

            if exchange is None:
                exchange = self._get_exchange_for_symbol(symbol)

            if self.contract_master:
                token, found_symbol = self._get_token_from_master(tradingsymbol)
                if token:
                    self.symbol_token_cache[symbol] = token
                    return token

            time_since_last_call = time.time() - self.last_api_call_time
            if time_since_last_call < self.api_call_delay:
                time.sleep(self.api_call_delay - time_since_last_call)

            try:
                search_result = self.smart_api.searchScrip(
                    exchange=exchange,
                    searchscrip=tradingsymbol,
                )
                self.last_api_call_time = time.time()
            except Exception as e:
                error_msg = str(e).lower()
                if "rate" in error_msg or "access denied" in error_msg:
                    time.sleep(1)
                    search_result = self.smart_api.searchScrip(
                        exchange=exchange,
                        searchscrip=tradingsymbol,
                    )
                    self.last_api_call_time = time.time()
                else:
                    raise

            if not search_result.get("status") or not search_result.get("data"):
                match = re.match(
                    r"NIFTY(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d+)(CE|PE)$",
                    tradingsymbol,
                )
                if match:
                    day, month, year_2digit, strike, opt_type = match.groups()
                    year_4digit = f"20{year_2digit}" if int(year_2digit) < 50 else f"19{year_2digit}"
                    alt_symbol = f"NIFTY{day}{month}{year_4digit}{strike}{opt_type}"

                    time_since_last_call = time.time() - self.last_api_call_time
                    if time_since_last_call < self.api_call_delay:
                        time.sleep(self.api_call_delay - time_since_last_call)

                    try:
                        search_result = self.smart_api.searchScrip(
                            exchange=exchange,
                            searchscrip=alt_symbol,
                        )
                        self.last_api_call_time = time.time()
                        if search_result.get("status") and search_result.get("data"):
                            tradingsymbol = alt_symbol
                    except Exception as e:
                        error_msg = str(e).lower()
                        if "rate" in error_msg or "access denied" in error_msg:
                            search_result = {"status": False, "data": None}
                        else:
                            raise

            if search_result.get("status") and search_result.get("data"):
                for item in search_result["data"]:
                    if item.get("tradingsymbol") == tradingsymbol:
                        token = item.get("symboltoken")
                        if token:
                            self.symbol_token_cache[symbol] = token
                            return token

                match = re.search(r"(\d+)(CE|PE)$", tradingsymbol)
                if match:
                    target_strike = int(match.group(1))
                    target_type = match.group(2)

                    for item in search_result["data"]:
                        item_symbol = item.get("tradingsymbol", "")
                        item_match = re.search(r"(\d+)(CE|PE)$", item_symbol)
                        if item_match:
                            item_strike = int(item_match.group(1))
                            item_type = item_match.group(2)

                            if item_strike == target_strike and item_type == target_type:
                                expiry_pattern = re.search(
                                    r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2,4})",
                                    tradingsymbol,
                                )
                                item_expiry_pattern = re.search(
                                    r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2,4})",
                                    item_symbol,
                                )
                                if expiry_pattern and item_expiry_pattern:
                                    token = item.get("symboltoken")
                                    if token:
                                        self.symbol_token_cache[symbol] = token
                                        return token

            self.failed_lookups.add(symbol)
            return None

        except Exception as e:
            error_msg = str(e).lower()
            if "rate" in error_msg or "access denied" in error_msg or "exceeding access rate" in error_msg:
                time.sleep(2)
                self.failed_lookups.add(symbol)
            else:
                logger.error(f"Error getting token for {symbol}: {e}")
            return None

    def get_positions(self):
        """Get positions"""
        try:
            return self.smart_api.position()
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return None

    def place_order(self, symbol, side, qty, order_type="MARKET", price=0):
        """Place order"""
        try:
            if ":" in symbol:
                parts = symbol.split(":")
            elif "|" in symbol:
                parts = symbol.split("|")
            else:
                parts = ["NSE", symbol]

            exchange = self._get_exchange_for_symbol(symbol)
            tradingsymbol = parts[1]

            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": tradingsymbol,
                "symboltoken": self._get_option_token(symbol),
                "transactiontype": "BUY" if side == "BUY" else "SELL",
                "exchange": exchange,
                "ordertype": order_type,
                "producttype": "INTRADAY",
                "duration": "DAY",
                "price": str(price) if order_type == "LIMIT" else "0",
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(qty),
            }

            return self.smart_api.placeOrder(order_params)
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None

    def get_option_symbol(self, expiry_date, strike_price, option_type="CE"):
        """Generate Angel option symbol (DDMMMYY format)"""
        day = expiry_date.strftime("%d")
        month = expiry_date.strftime("%b").upper()
        year_2digit = expiry_date.strftime("%y")

        expiry_formatted = f"{day}{month}{year_2digit}"
        return f"NSE:NIFTY{expiry_formatted}{int(strike_price)}{option_type}"

    def connect_websocket(self, symbols, handler):
        """Connect to Angel WebSocket"""
        logger.warning("Angel WebSocket not implemented. Using polling.")
        return None, None