import os
import math
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas as pd
import json
import logging
from pathlib import Path
import sqlite3
import time
from abc import ABC, abstractmethod
import requests
import asyncio

# Optional Fyers imports
try:
    from fyers_apiv3 import fyersModel
    FYERS_AVAILABLE = True
except ImportError:
    FYERS_AVAILABLE = False

try:
    from fyers_apiv3.FyersWebsocket import data_ws
    FYERS_WEBSOCKET_AVAILABLE = True
except ImportError:
    FYERS_WEBSOCKET_AVAILABLE = False

# Optional Angel Smart API imports
try:
    from SmartApi import SmartConnect
    import pyotp
    ANGEL_AVAILABLE = True
except ImportError:
    ANGEL_AVAILABLE = False

# Load environment variables
load_dotenv()

# =========================================================================
# ⚙️ CONFIGURATION
# =========================================================================
# Broker Selection: 'fyers' or 'angel'
BROKER = os.getenv('BROKER', 'fyers').lower()

# Fyers Configuration
FYERS_CLIENT_ID = os.getenv('FYERS_CLIENT_ID') or os.getenv('CLIENT_ID')
FYERS_ACCESS_TOKEN = os.getenv('FYERS_ACCESS_TOKEN') or os.getenv('ACCESS_TOKEN')

# Angel Smart API Configuration
ANGEL_API_KEY = os.getenv('ANGEL_API_KEY')
ANGEL_CLIENT_CODE = os.getenv('ANGEL_CLIENT_CODE')
ANGEL_MPIN = os.getenv('ANGEL_MPIN') or os.getenv('ANGEL_PASSWORD')  # Support both for backward compatibility
ANGEL_TOTP_SECRET = os.getenv('ANGEL_TOTP_SECRET')
ANGEL_LOAD_CONTRACT_MASTER = os.getenv('ANGEL_LOAD_CONTRACT_MASTER', 'true').lower() == 'true'  # Set to 'false' to disable master file download

# Nifty configuration
NIFTY_SYMBOL = "NSE:NIFTY50-INDEX"
STRIKE_INTERVAL = 50  # Nifty options have 50 point intervals
NUM_STRIKES_ABOVE_BELOW = 5  # Default: 5 strikes above/below
FAST_ENTRY_MODE = True  # When True: Smart expanding search for strikes with premiums in range, defer DB/logging
FAST_ENTRY_MAX_STRIKES = 4  # Maximum strikes to expand to in fast mode (will stop early if premiums found)
USE_ASYNC_FETCHING = True  # When True: Use async/await for concurrent API calls (much faster)
ASYNC_CONCURRENT_LIMIT = 10  # Maximum concurrent API calls (rate limiting)

# Algo Trading Configuration
MIN_PREMIUM = 43.0  # Minimum premium for strike selection
MAX_PREMIUM = 57.0  # Maximum premium for strike selection
MAX_TOTAL_PREMIUM = float(os.getenv('MAX_TOTAL_PREMIUM', '112.0'))  # Maximum total premium (call + put) - configurable via .env
TARGET_PROFIT_POINTS = 7.0  # Target profit in points to exit
VOLUME_RATIO_THRESHOLD = 2.0  # Volume ratio threshold (2x)

# Nifty Pre-Open Price Configuration
# Set this in .env before 09:15 AM to use pre-open price immediately when algo starts
# Format: NIFTY_PREOPEN_PRICE=25450.00 (use actual pre-open price)
NIFTY_PREOPEN_PRICE = os.getenv('NIFTY_PREOPEN_PRICE')  # Can be None if not set
if NIFTY_PREOPEN_PRICE:
    try:
        NIFTY_PREOPEN_PRICE = float(NIFTY_PREOPEN_PRICE)
    except (ValueError, TypeError):
        NIFTY_PREOPEN_PRICE = None
        print("⚠️  Warning: Invalid NIFTY_PREOPEN_PRICE in .env, ignoring...")

# Dry Run Mode - Set to False to enable actual trading
DRY_RUN = True  # When True: Log only, no orders. When False: Create actual trades
LOG_DIR = "algo_logs"  # Directory to save logs

# Data Storage Configuration
DATA_SNAPSHOT_INTERVAL = 30  # Store data every N seconds (30 = every 30 seconds)
USE_DATABASE = True  # Use SQLite database for efficient storage (recommended)
DATABASE_NAME = "algo_trading_data.db"  # SQLite database filename

# Trading Configuration
MAX_TRADES_PER_DAY = 1  # Maximum number of trades per day (1 = stop after first trade, 0 = unlimited)
CONTINUE_LOGGING_AFTER_TRADE = True  # Continue logging data even after max trades reached

# Entry Condition Bypass (for testing)
BYPASS_ENTRY_CONDITION = True  # Set to True to bypass volume condition check (for testing data collection)

# WebSocket Configuration
USE_WEBSOCKET = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'  # Set to 'false' to disable WebSocket and use API polling
API_POLLING_INTERVAL = float(os.getenv('API_POLLING_INTERVAL', '1.0'))  # Polling interval in seconds (default: 1 second)

# Strike Search Retry Configuration
STRIKE_SEARCH_RETRY_INTERVAL = float(os.getenv('STRIKE_SEARCH_RETRY_INTERVAL', '5.0'))  # Wait time between retries in seconds (default: 5 seconds)
STRIKE_SEARCH_MAX_RETRIES = int(os.getenv('STRIKE_SEARCH_MAX_RETRIES', '0'))  # Maximum retries (0 = unlimited, keep retrying)

# =========================================================================
# 🔌 BROKER ABSTRACTION LAYER
# =========================================================================

class BrokerInterface(ABC):
    """Abstract base class for broker implementations"""
    
    @abstractmethod
    def initialize(self):
        """Initialize broker connection and authenticate"""
        pass
    
    @abstractmethod
    def get_profile(self):
        """Get user profile information"""
        pass
    
    @abstractmethod
    def get_spot_price(self, symbol):
        """Get spot price for a symbol"""
        pass
    
    @abstractmethod
    def get_option_quote(self, symbol):
        """Get real-time quote for an option symbol"""
        pass
    
    @abstractmethod
    def get_positions(self):
        """Get current positions"""
        pass
    
    @abstractmethod
    def place_order(self, symbol, side, qty, order_type="MARKET", price=0):
        """Place an order"""
        pass
    
    @abstractmethod
    def get_option_symbol(self, expiry_date, strike_price, option_type):
        """Generate option symbol for the broker"""
        pass
    
    @abstractmethod
    def connect_websocket(self, symbols, handler):
        """Connect to WebSocket for real-time data"""
        pass

class FyersBroker(BrokerInterface):
    """Fyers API broker implementation"""
    
    def __init__(self):
        self.client_id = FYERS_CLIENT_ID
        self.access_token = FYERS_ACCESS_TOKEN
        self.model = None
        self.clean_token = None
        
    def initialize(self):
        """Initialize Fyers API connection"""
        if not FYERS_AVAILABLE:
            raise ImportError("fyers-apiv3 package not installed. Install with: pip install fyers-apiv3")
        
        if not self.client_id or not self.access_token:
            raise ValueError("FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN must be set in .env file")
        
        # Format token correctly
        if ':' in self.access_token:
            token_parts = self.access_token.split(':', 1)
            self.clean_token = token_parts[1] if len(token_parts) == 2 else self.access_token
        else:
            self.clean_token = self.access_token
        
        self.model = fyersModel.FyersModel(
            client_id=self.client_id,
            token=self.clean_token,
            is_async=False,
            log_path=os.getcwd()
        )
        
        # Verify connection
        profile = self.get_profile()
        if profile.get("code") == 200 or profile.get('s') == 'ok':
            return True
        else:
            raise Exception(f"Fyers API Login Failed: {profile.get('message', 'Unknown error')}")
    
    def get_profile(self):
        """Get user profile"""
        return self.model.get_profile()
    
    def get_spot_price(self, symbol):
        """Get Nifty spot price"""
        try:
            data = {"symbols": symbol}
            response = self.model.quotes(data=data)
            if response.get('s') == 'ok' and response.get('d'):
                return response['d'][0]['v']['lp']
            return None
        except Exception as e:
            logger.error(f"Error fetching spot price: {e}")
            return None
    
    def get_option_quote(self, symbol):
        """Get option quote"""
        try:
            data = {"symbols": symbol}
            response = self.model.quotes(data=data)
            if response.get('s') == 'ok' and response.get('d'):
                return response['d'][0].get('v', {})
            return None
        except Exception as e:
            return None
    
    def get_positions(self):
        """Get positions"""
        try:
            return self.model.positions()
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return None
    
    def place_order(self, symbol, side, qty, order_type="MARKET", price=0):
        """Place order"""
        try:
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "type": 2 if order_type == "LIMIT" else 1,  # 1=Market, 2=Limit
                "side": 1 if side == "BUY" else -1,
                "productType": "INTRADAY",
                "limitPrice": price,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": "False"
            }
            return self.model.place_order(data=order_data)
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None
    
    def get_option_symbol(self, expiry_date, strike_price, option_type="CE"):
        """Generate Fyers option symbol (YYMDD format)"""
        year = expiry_date.strftime('%y')
        month = str(expiry_date.month)  # No leading zero
        day = expiry_date.strftime('%d')
        expiry_formatted = f"{year}{month}{day}"
        return f"NSE:NIFTY{expiry_formatted}{int(strike_price)}{option_type}"
    
    def connect_websocket(self, symbols, handler):
        """Connect to Fyers WebSocket"""
        # Check if WebSocket is disabled via configuration
        if not USE_WEBSOCKET:
            logger.info("WebSocket disabled via USE_WEBSOCKET=false. Will use API polling instead.")
            return None, None
        
        if not FYERS_WEBSOCKET_AVAILABLE:
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
                on_message=handler.on_message
            )
            fyers_ws.connect()
            fyers_ws.subscribe(symbols=symbols, data_type="symbolUpdate")
            return fyers_ws, handler
        except Exception as e:
            logger.error(f"Error connecting Fyers WebSocket: {e}")
            logger.info("Falling back to API polling due to WebSocket connection failure.")
            return None, None

class AngelBroker(BrokerInterface):
    """Angel Smart API broker implementation"""
    
    def __init__(self):
        self.api_key = ANGEL_API_KEY
        self.client_code = ANGEL_CLIENT_CODE
        self.mpin = ANGEL_MPIN
        self.totp_secret = ANGEL_TOTP_SECRET
        self.smart_api = None
        self.feed_token = None
        self.jwt_token = None
        self.symbol_token_cache = {}  # Cache for symbol tokens to avoid repeated lookups
        self.failed_lookups = set()  # Cache failed lookups to avoid immediate retries
        self.last_api_call_time = 0  # Track last API call time for rate limiting
        self.api_call_delay = 0.1  # Minimum delay between API calls (100ms - optimized for speed)
        self.contract_master = None  # Cache for Angel contract master file
        self.master_file_url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        self.master_cache_file = Path(LOG_DIR) / "angel_contract_master.json"
        self.master_cache_max_age_hours = 24  # Re-download if cache is older than 24 hours
        
    def initialize(self):
        """Initialize Angel Smart API connection"""
        if not ANGEL_AVAILABLE:
            raise ImportError("smartapi-python package not installed. Install with: pip install smartapi-python")
        
        if not all([self.api_key, self.client_code, self.mpin, self.totp_secret]):
            raise ValueError("ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN (or ANGEL_PASSWORD), and ANGEL_TOTP_SECRET must be set in .env file")
        
        self.smart_api = SmartConnect(api_key=self.api_key)
        
        # Generate TOTP code (6-digit number)
        totp_code = pyotp.TOTP(self.totp_secret).now()
        
        # Generate session using MPIN (Angel One now requires MPIN instead of password)
        data = self.smart_api.generateSession(
            clientCode=self.client_code,
            password=self.mpin,  # Note: parameter name is still 'password' but value should be MPIN
            totp=totp_code  # Pass the 6-digit code string, not the TOTP object
        )
        
        if data.get('status') and data.get('data'):
            self.jwt_token = data['data']['jwtToken']
            self.feed_token = self.smart_api.getfeedToken()
            
            # Load contract master file for token lookup (optional - only needed if marketDataFull fails)
            # Set ANGEL_LOAD_CONTRACT_MASTER=false in .env to disable
            if ANGEL_LOAD_CONTRACT_MASTER:
                self._load_contract_master()
            else:
                logger.info("ℹ️  Contract master file loading disabled (ANGEL_LOAD_CONTRACT_MASTER=false)")
                logger.info("   Using marketDataFull API only (no token lookup fallback)")
            
            return True
        else:
            error_msg = data.get('message', 'Unknown error')
            raise Exception(f"Angel API Login Failed: {error_msg}")
    
    def _load_contract_master(self):
        """Load Angel contract master file for token lookup (with local caching)"""
        try:
            # Check if cached file exists and is recent
            cache_valid = False
            if self.master_cache_file.exists():
                cache_age = time.time() - self.master_cache_file.stat().st_mtime
                cache_age_hours = cache_age / 3600
                if cache_age_hours < self.master_cache_max_age_hours:
                    cache_valid = True
                    logger.info(f"📦 Using cached contract master file (age: {cache_age_hours:.1f} hours)")
            
            # Load from cache if valid
            if cache_valid:
                try:
                    with open(self.master_cache_file, 'r', encoding='utf-8') as f:
                        self.contract_master = json.load(f)
                    logger.info(f"✅ Loaded {len(self.contract_master)} contracts from cache")
                    return
                except Exception as cache_error:
                    logger.warning(f"⚠️  Failed to load from cache: {cache_error}, downloading fresh...")
            
            # Download fresh file
            logger.info("📥 Downloading Angel contract master file (this may take a moment)...")
            response = requests.get(self.master_file_url, timeout=30)
            response.raise_for_status()
            self.contract_master = response.json()
            logger.info(f"✅ Downloaded {len(self.contract_master)} contracts from server")
            
            # Save to cache
            try:
                self.master_cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.master_cache_file, 'w', encoding='utf-8') as f:
                    json.dump(self.contract_master, f)
                logger.info(f"💾 Cached contract master file to: {self.master_cache_file}")
            except Exception as save_error:
                logger.warning(f"⚠️  Failed to save cache: {save_error} (will re-download next time)")
                
        except Exception as e:
            logger.warning(f"⚠️  Failed to load contract master file: {e}")
            logger.warning("Will fall back to searchScrip API for token lookup")
            self.contract_master = None
    
    def _get_token_from_master(self, tradingsymbol):
        """
        Get option token from contract master file
        JSON format: symbol is like NIFTY13JAN2625450CE (with year, matches our format)
        Returns: (token, symbol) or (None, None)
        """
        if not self.contract_master:
            return None, None
        
        try:
            # JSON has symbol field that matches our format: NIFTY13JAN2625450CE
            # Just match the symbol directly (fastest and most reliable)
            for row in self.contract_master:
                if (
                    row.get("name") == "NIFTY" and
                    row.get("instrumenttype") == "OPTIDX" and
                    row.get("symbol") == tradingsymbol
                ):
                    token = row.get("token")  # JSON uses "token" not "symboltoken"
                    found_symbol = row.get("symbol")
                    if token:
                        logger.debug(f"✅ Found in master: {found_symbol} -> token {token}")
                        return str(token), found_symbol
            
            logger.debug(f"No match found in master file for symbol: {tradingsymbol}")
            return None, None
            
        except Exception as e:
            logger.error(f"Error parsing symbol from master file: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None, None
    
    def get_profile(self):
        """Get user profile"""
        try:
            return self.smart_api.getProfile(self.jwt_token)
        except Exception as e:
            return {'status': False, 'message': str(e)}
    
    def get_spot_price(self, symbol):
        """Get Nifty spot price"""
        try:
            # Angel uses different symbol format: NSE_INDEX|Nifty 50
            # For Nifty index, use: NSE_INDEX|Nifty 50
            if "NIFTY50" in symbol or "NIFTY" in symbol:
                angel_symbol = "NSE_INDEX|Nifty 50"
            else:
                angel_symbol = symbol.replace(":", "_")
            
            ltp_data = self.smart_api.ltpData(
                exchange="NSE",
                tradingsymbol=angel_symbol.split("|")[-1],
                symboltoken=self._get_symbol_token(angel_symbol)
            )
            
            if ltp_data.get('status') and ltp_data.get('data'):
                return float(ltp_data['data']['ltp'])
            return None
        except Exception as e:
            logger.error(f"Error fetching spot price: {e}")
            return None
    
    def _get_symbol_token(self, symbol):
        """Get symbol token for Angel API (simplified - may need master file lookup)"""
        # This is a simplified version - in production, you'd load the master file
        # For Nifty 50 index, token is typically "99926000"
        if "Nifty 50" in symbol or "NIFTY50" in symbol:
            return "99926000"
        # For options, you'd need to parse the symbol and look up in master file
        return None
    
    def _get_exchange_for_symbol(self, symbol):
        """Determine the correct exchange for a symbol
        - NFO for options (contains CE or PE)
        - NSE for indices and stocks
        """
        # Check if it's an option (contains CE or PE)
        if "CE" in symbol or "PE" in symbol:
            return "NFO"  # NSE Futures and Options
        return "NSE"  # Default to NSE for indices/stocks
    
    def get_option_quote(self, symbol):
        """Get option quote using Angel Smart API"""
        try:
            # Parse Angel symbol format: NSE:NIFTY13JAN25700CE (no year)
            # Support both : and | for backward compatibility
            if ":" in symbol:
                parts = symbol.split(":")
            elif "|" in symbol:
                parts = symbol.split("|")
            else:
                parts = ["NSE", symbol]
            
            # For options, use NFO exchange instead of NSE
            # The symbol format is NSE:NIFTY... but the exchange should be NFO
            exchange = self._get_exchange_for_symbol(symbol)
            tradingsymbol = parts[1]
            
            logger.info(f"🔍 Fetching quote for {symbol}: exchange={exchange}, tradingsymbol={tradingsymbol}")
            
            # Strategy 1: Try marketDataFull first (works without token, provides full OHLCV)
            # This is the preferred method as it doesn't require token lookup
            try:
                # Rate limiting
                time_since_last_call = time.time() - self.last_api_call_time
                if time_since_last_call < self.api_call_delay:
                    time.sleep(self.api_call_delay - time_since_last_call)
                
                logger.debug(f"Trying marketDataFull for {symbol} on {exchange}")
                market_data = self.smart_api.marketDataFull(
                    exchange=exchange,
                    tradingsymbol=tradingsymbol
                )
                self.last_api_call_time = time.time()
                
                if market_data.get('status'):
                    if market_data.get('data'):
                        data = market_data['data']
                        # Convert to standard format
                        return {
                            'ltp': float(data.get('ltp', 0)),
                            'open': float(data.get('open', 0)),
                            'high': float(data.get('high', 0)),
                            'low': float(data.get('low', 0)),
                            'close': float(data.get('close', 0)),
                            'volume': int(data.get('volume', 0)),
                            'previousClose': float(data.get('previousClose', 0))
                        }
                    else:
                        logger.debug(f"marketDataFull returned status=True but no data for {symbol}")
                else:
                    error_msg = market_data.get('message', 'Unknown error')
                    logger.debug(f"marketDataFull failed for {symbol}: {error_msg}")
            except Exception as market_error:
                error_msg = str(market_error).lower()
                if 'rate' in error_msg or 'access denied' in error_msg or 'exceeding access rate' in error_msg:
                    logger.warning(f"Rate limit in marketDataFull for {symbol}, waiting 2 seconds...")
                    time.sleep(2)
                    # Retry once
                    try:
                        market_data = self.smart_api.marketDataFull(
                            exchange=exchange,
                            tradingsymbol=tradingsymbol
                        )
                        self.last_api_call_time = time.time()
                        if market_data.get('status') and market_data.get('data'):
                            data = market_data['data']
                            return {
                                'ltp': float(data.get('ltp', 0)),
                                'open': float(data.get('open', 0)),
                                'high': float(data.get('high', 0)),
                                'low': float(data.get('low', 0)),
                                'close': float(data.get('close', 0)),
                                'volume': int(data.get('volume', 0)),
                                'previousClose': float(data.get('previousClose', 0))
                            }
                    except Exception as retry_error:
                        logger.debug(f"Retry also failed: {retry_error}")
                else:
                    logger.debug(f"marketDataFull exception for {symbol}: {market_error}")
            
            # Strategy 2: Fallback to token-based ltpData (if marketDataFull fails)
            # Only try token lookup if marketDataFull failed
            # Use NFO exchange for options
            symbol_token = self._get_option_token(symbol, exchange, tradingsymbol)
            
            if symbol_token:
                try:
                    time_since_last_call = time.time() - self.last_api_call_time
                    if time_since_last_call < self.api_call_delay:
                        time.sleep(self.api_call_delay - time_since_last_call)
                    
                    ltp_data = self.smart_api.ltpData(
                        exchange=exchange,
                        tradingsymbol=tradingsymbol,
                        symboltoken=symbol_token
                    )
                    self.last_api_call_time = time.time()
                    
                    if ltp_data.get('status') and ltp_data.get('data'):
                        data = ltp_data['data']
                        # ltpData returns minimal data, so we'll use what's available
                        return {
                            'ltp': float(data.get('ltp', 0)),
                            'open': 0,  # ltpData doesn't provide OHLC
                            'high': 0,
                            'low': 0,
                            'close': float(data.get('ltp', 0)),  # Use LTP as close
                            'volume': 0,  # ltpData doesn't provide volume
                            'previousClose': float(data.get('previousClose', 0))
                        }
                except Exception as ltp_error:
                    logger.debug(f"ltpData failed for {symbol}: {ltp_error}")
            
            return None
        except Exception as e:
            logger.error(f"Error fetching option quote for {symbol}: {e}")
            return None
    
    def _get_option_token(self, symbol, exchange=None, tradingsymbol=None):
        """Get option token using contract master file (preferred) or searchScrip API (fallback)"""
        # Check cache first
        if symbol in self.symbol_token_cache:
            return self.symbol_token_cache[symbol]
        
        # Check if this symbol recently failed (avoid immediate retries)
        if symbol in self.failed_lookups:
            logger.debug(f"Skipping recently failed lookup for {symbol}")
            return None
        
        try:
            # Parse tradingsymbol if not provided
            # Support both : and | for backward compatibility
            if not tradingsymbol:
                if ":" in symbol:
                    parts = symbol.split(":")
                elif "|" in symbol:
                    parts = symbol.split("|")
                else:
                    parts = ["NSE", symbol]
                tradingsymbol = parts[1]
            
            # Determine correct exchange (NFO for options, NSE for others)
            if exchange is None:
                exchange = self._get_exchange_for_symbol(symbol)
            
            logger.debug(f"Looking up token for: {symbol}")
            
            # Strategy 1: Use contract master file (preferred - no API calls, no rate limits)
            if self.contract_master:
                token, found_symbol = self._get_token_from_master(tradingsymbol)
                if token:
                    self.symbol_token_cache[symbol] = token
                    logger.debug(f"✅ Found token {token} for {symbol} from master file")
                    return token
            
            # Strategy 2: Fallback to searchScrip API (if master file not available or lookup failed)
            logger.debug(f"Master file lookup failed, trying searchScrip API for {symbol}")
            
            # Rate limiting: Ensure minimum delay between API calls
            time_since_last_call = time.time() - self.last_api_call_time
            if time_since_last_call < self.api_call_delay:
                sleep_time = self.api_call_delay - time_since_last_call
                time.sleep(sleep_time)
            
            # Try searchScrip API
            try:
                search_result = self.smart_api.searchScrip(
                    exchange=exchange,
                    searchscrip=tradingsymbol
                )
                self.last_api_call_time = time.time()
            except Exception as e:
                error_msg = str(e).lower()
                if 'rate' in error_msg or 'access denied' in error_msg:
                    logger.warning(f"Rate limit hit for {symbol}, waiting 1 second...")
                    time.sleep(1)
                    # Retry once after delay
                    search_result = self.smart_api.searchScrip(
                        exchange=exchange,
                        searchscrip=tradingsymbol
                    )
                    self.last_api_call_time = time.time()
                else:
                    raise
            
            # Strategy 2: If that fails, try with 4-digit year format (only if first search failed)
            if not search_result.get('status') or not search_result.get('data'):
                import re
                # Try converting 2-digit year to 4-digit year
                match = re.match(r'NIFTY(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d+)(CE|PE)$', tradingsymbol)
                if match:
                    day, month, year_2digit, strike, opt_type = match.groups()
                    # Convert 2-digit year to 4-digit (assuming 20xx for years 00-99)
                    year_4digit = f"20{year_2digit}" if int(year_2digit) < 50 else f"19{year_2digit}"
                    alt_symbol = f"NIFTY{day}{month}{year_4digit}{strike}{opt_type}"
                    logger.debug(f"Trying alternative format with 4-digit year: {alt_symbol}")
                    
                    # Rate limiting before second API call
                    time_since_last_call = time.time() - self.last_api_call_time
                    if time_since_last_call < self.api_call_delay:
                        time.sleep(self.api_call_delay - time_since_last_call)
                    
                    try:
                        search_result = self.smart_api.searchScrip(
                            exchange=exchange,  # Use NFO for options
                            searchscrip=alt_symbol
                        )
                        self.last_api_call_time = time.time()
                        if search_result.get('status') and search_result.get('data'):
                            # Update the symbol format if this works
                            tradingsymbol = alt_symbol
                    except Exception as e:
                        error_msg = str(e).lower()
                        if 'rate' in error_msg or 'access denied' in error_msg:
                            logger.warning(f"Rate limit hit, skipping alternative search for {symbol}")
                            # Don't try Strategy 3 if we're hitting rate limits
                            search_result = {'status': False, 'data': None}
                        else:
                            raise
            
            # Strategy 3: Only try broad search if we haven't hit rate limits
            # (Skip this to avoid too many API calls)
            # Note: Broad search is disabled to prevent rate limiting
            
            if search_result.get('status') and search_result.get('data'):
                # Try exact match first
                for item in search_result['data']:
                    if item.get('tradingsymbol') == tradingsymbol:
                        token = item.get('symboltoken')
                        if token:
                            self.symbol_token_cache[symbol] = token
                            logger.info(f"✅ Found token for {symbol}: {token}")
                            return token
                
                # If exact match not found, try pattern matching
                import re
                match = re.search(r'(\d+)(CE|PE)$', tradingsymbol)
                if match:
                    target_strike = int(match.group(1))
                    target_type = match.group(2)
                    
                    logger.debug(f"Searching for strike={target_strike}, type={target_type}")
                    
                    # Find matching symbol by strike and type
                    for item in search_result['data']:
                        item_symbol = item.get('tradingsymbol', '')
                        item_match = re.search(r'(\d+)(CE|PE)$', item_symbol)
                        if item_match:
                            item_strike = int(item_match.group(1))
                            item_type = item_match.group(2)
                            
                            if item_strike == target_strike and item_type == target_type:
                                # Check if expiry pattern matches
                                expiry_pattern = re.search(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2,4})', tradingsymbol)
                                item_expiry_pattern = re.search(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2,4})', item_symbol)
                                
                                if expiry_pattern and item_expiry_pattern:
                                    token = item.get('symboltoken')
                                    if token:
                                        self.symbol_token_cache[symbol] = token
                                        logger.info(f"✅ Found token for {symbol}: {token} (matched with {item_symbol})")
                                        return token
            
            # If all searches failed, provide more detailed error info
            if search_result.get('status'):
                if search_result.get('data'):
                    # We got results but no match - show what we found
                    logger.warning(f"❌ Token lookup failed for {symbol}")
                    logger.warning(f"Search returned {len(search_result['data'])} results but no exact match")
                    # Show a few sample symbols to help debug format
                    if len(search_result['data']) > 0:
                        sample = search_result['data'][:5]
                        sample_symbols = [s.get('tradingsymbol', 'N/A') for s in sample]
                        logger.info(f"Sample symbols found: {sample_symbols}")
                        logger.info(f"Try matching your symbol format with these examples")
                else:
                    logger.warning(f"❌ Token lookup failed for {symbol} - search returned no results")
                    logger.warning(f"Possible reasons: market closed, symbol format incorrect, or contract doesn't exist yet")
            else:
                logger.warning(f"❌ Token lookup failed for {symbol} - search API returned error")
            
            # Cache failed lookup to avoid immediate retries
            self.failed_lookups.add(symbol)
            return None
        except Exception as e:
            error_msg = str(e).lower()
            if 'rate' in error_msg or 'access denied' in error_msg or 'exceeding access rate' in error_msg:
                logger.warning(f"Rate limit error for {symbol}: {e}")
                logger.warning("Waiting 2 seconds before continuing...")
                time.sleep(2)  # Wait longer on rate limit errors
                # Cache failed lookup to avoid immediate retries
                self.failed_lookups.add(symbol)
            else:
                logger.error(f"Error getting token for {symbol}: {e}")
                import traceback
                logger.debug(traceback.format_exc())
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
            # Parse symbol - support both : and | for backward compatibility
            if ":" in symbol:
                parts = symbol.split(":")
            elif "|" in symbol:
                parts = symbol.split("|")
            else:
                parts = ["NSE", symbol]
            
            # For options, use NFO exchange instead of NSE
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
                "quantity": str(qty)
            }
            
            return self.smart_api.placeOrder(order_params)
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None
    
    def get_option_symbol(self, expiry_date, strike_price, option_type="CE"):
        """Generate Angel option symbol (DDMMMYY format - matches JSON format)
        
        Format: NSE:NIFTY<DD><MON><YY><STRIKE><CE/PE>
        Example: NSE:NIFTY13JAN2625450CE (matches JSON symbol format)
        """
        day = expiry_date.strftime('%d')  # DD (e.g., 13)
        month = expiry_date.strftime('%b').upper()  # MON (e.g., JAN, FEB, etc.)
        year_2digit = expiry_date.strftime('%y')  # YY (e.g., 26)
        
        # Format: DDMMMYY (e.g., 13JAN26 for Jan 13, 2026) - matches JSON format
        expiry_formatted = f"{day}{month}{year_2digit}"
        # Format: NSE:NIFTY<DD><MON><YY><STRIKE><CE/PE> (matches JSON symbol: NIFTY13JAN2625450CE)
        symbol = f"NSE:NIFTY{expiry_formatted}{int(strike_price)}{option_type}"
        return symbol
    
    def connect_websocket(self, symbols, handler):
        """Connect to Angel WebSocket"""
        # Check if WebSocket is disabled via configuration
        if not USE_WEBSOCKET:
            logger.info("WebSocket disabled via USE_WEBSOCKET=false. Will use API polling instead.")
            return None, None
        
        # Angel Smart API WebSocket implementation
        # Note: Angel WebSocket setup is different from Fyers
        # This is a placeholder - full implementation would require Angel WebSocket library
        logger.warning("Angel WebSocket not yet fully implemented. Will use API polling instead.")
        return None, None

def get_broker():
    """Factory function to get the appropriate broker instance"""
    if BROKER == 'fyers':
        return FyersBroker()
    elif BROKER == 'angel':
        return AngelBroker()
    else:
        raise ValueError(f"Unknown broker: {BROKER}. Set BROKER=fyers or BROKER=angel in .env file")

# =========================================================================
# 📝 LOGGING SETUP
# =========================================================================

def setup_logging():
    """Setup logging for algo trading"""
    # Create log directory if it doesn't exist
    log_path = Path(LOG_DIR)
    log_path.mkdir(exist_ok=True)
    
    # Create log filename with timestamp
    log_filename = log_path / f"algo_trading_{datetime.now().strftime('%Y%m%d')}.log"
    
    # Configure logging with force=True to allow reconfiguration
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            logging.StreamHandler()  # Also print to console
        ],
        force=True  # Force reconfiguration if already configured
    )
    
    logger = logging.getLogger(__name__)
    
    # Log mode and session start
    logger.info("="*100)
    logger.info(f"ALGO TRADING SESSION STARTED - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("="*100)
    if DRY_RUN:
        logger.info("MODE: 🔶 DRY RUN - No actual orders will be placed")
    else:
        logger.info("MODE: 🔴 LIVE TRADING - Actual orders will be placed")
    logger.info("="*100)
    
    return logger

# Initialize logger (will be set by setup_logging)
logger = None

# Initialize database (will be set by setup_database)
db_conn = None
last_snapshot_time = time.time() - DATA_SNAPSHOT_INTERVAL  # Allow first snapshot immediately

# =========================================================================
# 💾 DATABASE FUNCTIONS
# =========================================================================

def setup_database():
    """Setup SQLite database for efficient data storage"""
    if not USE_DATABASE:
        return None
    
    log_path = Path(LOG_DIR)
    log_path.mkdir(exist_ok=True)
    db_path = log_path / DATABASE_NAME
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create options_data table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS options_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strike INTEGER,
            option_type TEXT,
            open_price REAL,
            high_price REAL,
            low_price REAL,
            close_price REAL,
            ltp REAL,
            volume INTEGER,
            prev_close REAL,
            delta REAL,
            gamma REAL,
            theta REAL,
            vega REAL,
            iv REAL
        )
    ''')
    
    # Create index for faster queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_timestamp ON options_data(timestamp)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_date_symbol ON options_data(date, symbol)
    ''')
    
    # Create nifty_spot table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nifty_spot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            spot_price REAL NOT NULL,
            atm_strike INTEGER
        )
    ''')
    
    # Create volume_analysis table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS volume_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            atm_strike INTEGER,
            otm_call_volume INTEGER,
            otm_put_volume INTEGER,
            call_put_ratio REAL,
            put_call_ratio REAL,
            entry_condition_met TEXT,
            condition_type TEXT
        )
    ''')
    
    # Migrate existing tables: Add missing columns if they don't exist
    _migrate_database_schema(cursor)
    
    conn.commit()
    # Note: logger might not be initialized yet, so we'll log later
    return conn

def _migrate_database_schema(cursor):
    """Add missing columns to existing database tables (migration)"""
    try:
        # Check if options_data table exists and get its columns
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='options_data'")
        if cursor.fetchone():
            # Get existing columns
            cursor.execute("PRAGMA table_info(options_data)")
            existing_columns = [row[1] for row in cursor.fetchall()]
            
            # Columns to add if missing
            columns_to_add = [
                ('delta', 'REAL'),
                ('gamma', 'REAL'),
                ('theta', 'REAL'),
                ('vega', 'REAL'),
                ('iv', 'REAL')
            ]
            
            # Add missing columns
            for column_name, column_type in columns_to_add:
                if column_name not in existing_columns:
                    try:
                        cursor.execute(f'ALTER TABLE options_data ADD COLUMN {column_name} {column_type}')
                        print(f"✅ Added missing column '{column_name}' to options_data table")
                    except sqlite3.OperationalError as e:
                        # Column might already exist (race condition), ignore
                        if 'duplicate column' not in str(e).lower():
                            print(f"⚠️  Could not add column '{column_name}': {e}")
    except Exception as e:
        print(f"⚠️  Error during database migration: {e}")
        # Don't fail setup if migration fails - table might be new

def save_options_data_to_db(conn, all_option_data, timestamp_str, date_str):
    """Save options data to database efficiently"""
    if not conn or not USE_DATABASE:
        return
    
    try:
        cursor = conn.cursor()
        
        # Check if Greek columns exist (for backward compatibility)
        cursor.execute("PRAGMA table_info(options_data)")
        columns = [row[1] for row in cursor.fetchall()]
        has_greeks = all(col in columns for col in ['delta', 'gamma', 'theta', 'vega', 'iv'])
        
        for option in all_option_data:
            if has_greeks:
                # Insert with all columns including Greeks
                cursor.execute('''
                    INSERT INTO options_data 
                    (timestamp, date, symbol, strike, option_type, open_price, high_price, 
                     low_price, close_price, ltp, volume, prev_close, delta, gamma, theta, vega, iv)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    timestamp_str,
                    date_str,
                    option.get('Symbol', ''),
                    option.get('Strike', 0),
                    option.get('Option_Type', ''),
                    option.get('Open', 0),
                    option.get('High', 0),
                    option.get('Low', 0),
                    option.get('Close', 0),
                    option.get('LTP', 0),
                    option.get('Volume', 0),
                    option.get('Prev_Close', 0),
                    option.get('Delta'),
                    option.get('Gamma'),
                    option.get('Theta'),
                    option.get('Vega'),
                    option.get('IV')
                ))
            else:
                # Fallback: Insert without Greek columns (for old database schema)
                cursor.execute('''
                    INSERT INTO options_data 
                    (timestamp, date, symbol, strike, option_type, open_price, high_price, 
                     low_price, close_price, ltp, volume, prev_close)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    timestamp_str,
                    date_str,
                    option.get('Symbol', ''),
                    option.get('Strike', 0),
                    option.get('Option_Type', ''),
                    option.get('Open', 0),
                    option.get('High', 0),
                    option.get('Low', 0),
                    option.get('Close', 0),
                    option.get('LTP', 0),
                    option.get('Volume', 0),
                    option.get('Prev_Close', 0)
                ))
        conn.commit()
    except Exception as e:
        if logger:
            logger.error(f"Error saving options data to database: {e}")
        else:
            print(f"❌ Error saving options data to database: {e}")

def save_nifty_spot_to_db(conn, nifty_spot, atm_strike, timestamp_str, date_str):
    """Save Nifty spot price to database"""
    global logger
    if not conn or not USE_DATABASE:
        return
    
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO nifty_spot (timestamp, date, spot_price, atm_strike)
            VALUES (?, ?, ?, ?)
        ''', (timestamp_str, date_str, nifty_spot, atm_strike))
        conn.commit()
    except Exception as e:
        if logger:
            logger.error(f"Error saving Nifty spot to database: {e}")

def save_volume_analysis_to_db(conn, atm_strike, otm_call_volume, otm_put_volume, 
                               condition_met, condition_type, timestamp_str, date_str):
    """Save volume analysis to database"""
    global logger
    if not conn or not USE_DATABASE:
        return
    
    try:
        call_put_ratio = otm_call_volume / otm_put_volume if otm_put_volume > 0 else 0
        put_call_ratio = otm_put_volume / otm_call_volume if otm_call_volume > 0 else 0
        
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO volume_analysis 
            (timestamp, date, atm_strike, otm_call_volume, otm_put_volume, 
             call_put_ratio, put_call_ratio, entry_condition_met, condition_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            timestamp_str, date_str, atm_strike, otm_call_volume, otm_put_volume,
            call_put_ratio, put_call_ratio, str(condition_met), condition_type
        ))
        conn.commit()
    except Exception as e:
        if logger:
            logger.error(f"Error saving volume analysis to database: {e}")

def should_take_snapshot():
    """Check if it's time to take a data snapshot based on interval"""
    global last_snapshot_time
    current_time = time.time()
    
    if current_time - last_snapshot_time >= DATA_SNAPSHOT_INTERVAL:
        last_snapshot_time = current_time
        return True
    return False

def get_nifty_close_price(conn, date_str):
    """Get Nifty spot close price for a given date"""
    global logger
    if not conn or not USE_DATABASE:
        return None
    
    try:
        cursor = conn.cursor()
        # Get the last entry for the date (closest to market close)
        cursor.execute('''
            SELECT spot_price, timestamp FROM nifty_spot 
            WHERE date = ? 
            ORDER BY timestamp DESC 
            LIMIT 1
        ''', (date_str,))
        result = cursor.fetchone()
        if result:
            return result[0], result[1]  # spot_price, timestamp
        return None, None
    except Exception as e:
        if logger:
            logger.error(f"Error getting Nifty close price: {e}")
        return None, None

def log_end_of_day_summary(conn, date_str):
    """Log end of day summary"""
    global logger
    if not logger:
        return
    
    logger.info("=" * 100)
    logger.info("END OF DAY SUMMARY")
    logger.info("=" * 100)
    
    # Get Nifty close
    nifty_close, close_time = get_nifty_close_price(conn, date_str)
    if nifty_close:
        logger.info(f"NIFTY SPOT CLOSE: ₹{nifty_close:.2f} at {close_time}")
    else:
        logger.info("NIFTY SPOT CLOSE: Not available")
    
    # Get volume analysis summary
    if conn and USE_DATABASE:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM volume_analysis 
                WHERE date = ? 
                ORDER BY timestamp DESC 
                LIMIT 1
            ''', (date_str,))
            result = cursor.fetchone()
            if result:
                logger.info(f"Final ATM Strike: {result[2]}")
                logger.info(f"Final OTM Call Volume: {result[3]:,.0f}")
                logger.info(f"Final OTM Put Volume: {result[4]:,.0f}")
                logger.info(f"Final Call/Put Ratio: {result[5]:.2f}")
        except Exception as e:
            logger.error(f"Error getting volume summary: {e}")
    
    logger.info("=" * 100)

# =========================================================================
# 🔧 HELPER FUNCTIONS
# =========================================================================

def round_to_nearest_50(price):
    """Round price to nearest 50 (ATM strike calculation)"""
    return round(price / STRIKE_INTERVAL) * STRIKE_INTERVAL

def get_strike_prices(spot_price, fast_mode=False):
    """
    Calculate ATM strike and surrounding strikes
    Returns: (atm_strike, strikes_above, strikes_below, all_strikes)
    """
    atm_strike = round_to_nearest_50(spot_price)
    num_strikes = FAST_ENTRY_MAX_STRIKES if fast_mode else NUM_STRIKES_ABOVE_BELOW
    strikes_above = [atm_strike + (i * STRIKE_INTERVAL) for i in range(1, num_strikes + 1)]
    strikes_below = [atm_strike - (i * STRIKE_INTERVAL) for i in range(1, num_strikes + 1)]
    all_strikes = sorted(strikes_below + [atm_strike] + strikes_above)
    return atm_strike, strikes_above, strikes_below, all_strikes

def fetch_strikes_smart(broker, expiry_date, atm_strike, nifty_spot):
    """
    Smart strike fetching: Expands outward from ATM and stops when strikes with
    premiums in 43-57 range are found. This ensures we find the right strikes
    while minimizing API calls.
    
    Returns: (all_option_data, option_symbols, found_matching_premiums)
    """
    all_option_data = []
    option_symbols = []
    found_call_in_range = False
    found_put_in_range = False
    
    # Start with ATM, then expand outward level by level
    max_level = FAST_ENTRY_MAX_STRIKES
    
    for level in range(max_level + 1):  # 0 = ATM, 1 = ±1, 2 = ±2, etc.
        strikes_to_fetch = []
        
        if level == 0:
            # Level 0: Just ATM
            strikes_to_fetch = [atm_strike]
        else:
            # Level 1+: Add strikes at this distance
            strikes_to_fetch = [
                atm_strike + (level * STRIKE_INTERVAL),  # Above
                atm_strike - (level * STRIKE_INTERVAL)  # Below
            ]
        
        # Fetch data for strikes at this level
        for strike in strikes_to_fetch:
            # Call Option
            ce_symbol = get_option_symbol(expiry_date, strike, "CE", broker)
            if ce_symbol not in [opt.get('Symbol', '') for opt in all_option_data if opt.get('Option_Type') == 'CE']:
                option_symbols.append(ce_symbol)
                ce_data = get_option_ohlcv_realtime(broker, ce_symbol)
                if ce_data:
                    ce_data['Strike'] = strike
                    ce_data['Option_Type'] = 'CE'
                    all_option_data.append(ce_data)
                    
                    # Check if premium is in range
                    premium = ce_data.get('LTP', 0)
                    if MIN_PREMIUM <= premium <= MAX_PREMIUM:
                        found_call_in_range = True
                
                # Rate limiting
                if isinstance(broker, AngelBroker):
                    time.sleep(0.1)
            
            # Put Option
            pe_symbol = get_option_symbol(expiry_date, strike, "PE", broker)
            if pe_symbol not in [opt.get('Symbol', '') for opt in all_option_data if opt.get('Option_Type') == 'PE']:
                option_symbols.append(pe_symbol)
                pe_data = get_option_ohlcv_realtime(broker, pe_symbol)
                if pe_data:
                    pe_data['Strike'] = strike
                    pe_data['Option_Type'] = 'PE'
                    all_option_data.append(pe_data)
                    
                    # Check if premium is in range
                    premium = pe_data.get('LTP', 0)
                    if MIN_PREMIUM <= premium <= MAX_PREMIUM:
                        found_put_in_range = True
                
                # Rate limiting
                if isinstance(broker, AngelBroker):
                    time.sleep(0.1)
        
        # If we found both call and put in range, we can stop early
        if found_call_in_range and found_put_in_range:
            # Check if we can form a valid combination
            entry_data = find_entry_strikes(all_option_data, nifty_spot)
            if entry_data:
                break  # Found valid combination, stop fetching
    
    return all_option_data, option_symbols, (found_call_in_range and found_put_in_range)

async def fetch_strikes_smart_async(broker, expiry_date, atm_strike, nifty_spot):
    """
    Async version: Smart strike fetching with concurrent API calls.
    Expands outward from ATM and stops when strikes with premiums in 43-57 range are found.
    Uses asyncio.gather to fetch multiple strikes concurrently for much faster execution.
    Uses semaphore for rate limiting to avoid overwhelming the API.
    
    Returns: (all_option_data, option_symbols, found_matching_premiums)
    """
    all_option_data = []
    option_symbols = []
    found_call_in_range = False
    found_put_in_range = False
    fetched_symbols = set()  # Track fetched symbols to avoid duplicates
    
    # Create semaphore for rate limiting concurrent requests
    semaphore = asyncio.Semaphore(ASYNC_CONCURRENT_LIMIT)
    
    async def fetch_with_limit(symbol, strike, option_type):
        """Fetch with semaphore to limit concurrent requests"""
        async with semaphore: # Wait here if 10 requests are already running
            return await get_option_ohlcv_realtime_async(broker, symbol), strike, option_type
    
    # Start with ATM, then expand outward level by level
    max_level = FAST_ENTRY_MAX_STRIKES
    
    for level in range(max_level + 1):  # 0 = ATM, 1 = ±1, 2 = ±2, etc.
        strikes_to_fetch = []
        
        if level == 0:
            # Level 0: Just ATM
            strikes_to_fetch = [atm_strike]
        else:
            # Level 1+: Add strikes at this distance
            strikes_to_fetch = [
                atm_strike + (level * STRIKE_INTERVAL),  # Above
                atm_strike - (level * STRIKE_INTERVAL)  # Below
            ]
        
        # Prepare all symbols to fetch at this level
        fetch_tasks = []
        
        for strike in strikes_to_fetch:
            # Call Option
            ce_symbol = get_option_symbol(expiry_date, strike, "CE", broker)
            if ce_symbol not in fetched_symbols:
                fetched_symbols.add(ce_symbol)
                option_symbols.append(ce_symbol)
                fetch_tasks.append(fetch_with_limit(ce_symbol, strike, 'CE'))
            
            # Put Option
            pe_symbol = get_option_symbol(expiry_date, strike, "PE", broker)
            if pe_symbol not in fetched_symbols:
                fetched_symbols.add(pe_symbol)
                option_symbols.append(pe_symbol)
                fetch_tasks.append(fetch_with_limit(pe_symbol, strike, 'PE'))
        
        # Fetch all strikes at this level concurrently (with rate limiting)
        if fetch_tasks:
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            
            # Process results
            for result in results:
                if isinstance(result, Exception):
                    continue  # Skip errors
                
                data, strike, option_type = result
                if data:
                    data['Strike'] = strike
                    data['Option_Type'] = option_type
                    all_option_data.append(data)
                    
                    # Check if premium is in range
                    premium = data.get('LTP', 0)
                    if MIN_PREMIUM <= premium <= MAX_PREMIUM:
                        if option_type == 'CE':
                            found_call_in_range = True
                        else:
                            found_put_in_range = True
        
        # If we found both call and put in range, check if we can form a valid combination
        if found_call_in_range and found_put_in_range:
            entry_data = find_entry_strikes(all_option_data, nifty_spot)
            if entry_data:
                break  # Found valid combination, stop fetching
    
    return all_option_data, option_symbols, (found_call_in_range and found_put_in_range)

def get_nifty_spot_price(broker):
    """Get current Nifty spot price using broker abstraction"""
    try:
        return broker.get_spot_price(NIFTY_SYMBOL)
    except Exception as e:
        print(f"Exception while fetching Nifty spot: {e}")
        return None

def get_option_symbol(expiry_date, strike_price, option_type="CE", broker=None):
    """
    Generate option symbol format based on broker
    If broker is None, uses configured BROKER from env
    """
    if broker is None:
        broker = get_broker()
    
    return broker.get_option_symbol(expiry_date, strike_price, option_type)

def get_option_ohlcv_realtime(broker, option_symbol):
    """Get real-time OHLCV data for an option contract using broker abstraction"""
    try:
        quote_data = broker.get_option_quote(option_symbol)
        if not quote_data:
            return None
        
        # Handle different broker response formats
        if isinstance(broker, FyersBroker):
            # Fyers format
            open_price = quote_data.get('open_price', 0)
            high_price = quote_data.get('high_price', 0)
            low_price = quote_data.get('low_price', 0)
            ltp = quote_data.get('lp', 0)
            volume = quote_data.get('volume', 0)
            prev_close = quote_data.get('prev_close_price', 0)
            delta = quote_data.get('delta')
            gamma = quote_data.get('gamma')
            theta = quote_data.get('theta')
            vega = quote_data.get('vega')
            iv = quote_data.get('iv')
        elif isinstance(broker, AngelBroker):
            # Angel format
            ltp = float(quote_data.get('ltp', 0))
            open_price = float(quote_data.get('open', 0))
            high_price = float(quote_data.get('high', 0))
            low_price = float(quote_data.get('low', 0))
            volume = int(quote_data.get('volume', 0))
            prev_close = float(quote_data.get('previousClose', 0))
            # Angel may not provide Greeks in standard quote
            delta = None
            gamma = None
            theta = None
            vega = None
            iv = None
        else:
            return None
        
        # Use prev_close as open if open is 0 (market just opened)
        if open_price == 0 and prev_close > 0:
            open_price = prev_close
        
        ohlcv = {
            'Symbol': option_symbol,
            'Open': open_price,
            'High': high_price,
            'Low': low_price,
            'Close': ltp,  # Use LTP as current close
            'Volume': volume,
            'LTP': ltp,
            'Prev_Close': prev_close,
            'Delta': delta,
            'Gamma': gamma,
            'Theta': theta,
            'Vega': vega,
            'IV': iv,
            'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        return ohlcv
    except Exception as e:
        return None

async def get_option_ohlcv_realtime_async(broker, option_symbol):
    """Async version: Get real-time OHLCV data for an option contract using broker abstraction"""
    # Run the blocking call in a thread pool to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    try:
        # Use to_thread (Python 3.9+) or run_in_executor for older versions
        if hasattr(asyncio, 'to_thread'):
            result = await asyncio.to_thread(get_option_ohlcv_realtime, broker, option_symbol)
        else:
            result = await loop.run_in_executor(None, get_option_ohlcv_realtime, broker, option_symbol)
        return result
    except Exception as e:
        return None

def get_historical_option_data(fyers_model, option_symbol, start_date, end_date, interval="D"):
    """Get historical OHLCV data for an option"""
    try:
        data = {
            "symbol": option_symbol,
            "resolution": interval,
            "date_format": "1",  # YYYY-MM-DD format
            "range_from": start_date,
            "range_to": end_date,
            "cont_flag": "1"
        }
        
        response = fyers_model.history(data=data)
        
        if response.get("code") == 200 and response.get("candles"):
            candles = response["candles"]
            if candles:
                df = pd.DataFrame(candles, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['Date'] = pd.to_datetime(df['Date'], unit='s', utc=True)
                df.set_index('Date', inplace=True)
                return df
            else:
                return pd.DataFrame()
        else:
            # Don't print error for every failed symbol (too verbose)
            return pd.DataFrame()
    except Exception as e:
        # Don't print error for every failed symbol
        return pd.DataFrame()

def get_nearest_expiry():
    """Get the nearest Tuesday (Nifty options expiry)"""
    today = datetime.now()
    
    # Calculate days until next Tuesday
    # Tuesday = 1 in Python's weekday (Monday=0, Tuesday=1, ..., Sunday=6)
    days_until_tuesday = (1 - today.weekday()) % 7
    
    # If it's Tuesday and after 3 PM, get next Tuesday
    if days_until_tuesday == 0 and today.hour >= 15:
        days_until_tuesday = 7
    
    # Calculate the expiry date
    nearest_tuesday = today + timedelta(days=days_until_tuesday)
    nearest_tuesday = nearest_tuesday.replace(hour=0, minute=0, second=0, microsecond=0)
    
    return nearest_tuesday

# =========================================================================
# 🤖 ALGO TRADING FUNCTIONS
# =========================================================================

class AlgoTrade:
    """Class to manage algo trading state and execution"""
    def __init__(self):
        self.entry_call_strike = None
        self.entry_put_strike = None
        self.entry_call_price = 0.0
        self.entry_put_price = 0.0
        self.entry_total_price = 0.0
        self.entry_time = None
        self.is_position_open = False
        self.call_symbol = None
        self.put_symbol = None
        self.trade_completed = False  # Track if a trade has been completed today
        self.call_order_id = None
        self.put_order_id = None
    
    def restore_from_positions(self, call_symbol, put_symbol, call_strike, put_strike, call_price, put_price):
        """Restore trade state from existing positions"""
        self.call_symbol = call_symbol
        self.put_symbol = put_symbol
        self.entry_call_strike = call_strike
        self.entry_put_strike = put_strike
        self.entry_call_price = call_price
        self.entry_put_price = put_price
        self.entry_total_price = call_price + put_price
        self.entry_time = datetime.now()  # Use current time as we don't have original entry time
        self.is_position_open = True
        self.trade_completed = False  # Reset to allow exit monitoring
        
    def enter_trade(self, call_strike, put_strike, call_price, put_price, call_symbol, put_symbol, broker=None):
        """Record trade entry and place orders if not in dry run"""
        self.entry_call_strike = call_strike
        self.entry_put_strike = put_strike
        self.entry_call_price = call_price
        self.entry_put_price = put_price
        self.entry_total_price = call_price + put_price
        self.entry_time = datetime.now()
        self.is_position_open = True
        self.call_symbol = call_symbol
        self.put_symbol = put_symbol
        self.call_order_id = None
        self.put_order_id = None
        
        mode_text = "🔶 [DRY RUN] " if DRY_RUN else "🔴 [LIVE] "
        
        print(f"\n{'='*80}")
        print(f"{mode_text}TRADE ENTERED")
        print(f"{'='*80}")
        print(f"Call Strike: {call_strike} @ ₹{call_price:.2f}")
        print(f"Put Strike: {put_strike} @ ₹{put_price:.2f}")
        print(f"Total Entry Price: ₹{self.entry_total_price:.2f}")
        print(f"Entry Time: {self.entry_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Log trade entry
        logger.info(f"{mode_text}TRADE ENTERED")
        logger.info(f"Call Strike: {call_strike} @ ₹{call_price:.2f} | Symbol: {call_symbol}")
        logger.info(f"Put Strike: {put_strike} @ ₹{put_price:.2f} | Symbol: {put_symbol}")
        logger.info(f"Total Entry Price: ₹{self.entry_total_price:.2f}")
        logger.info(f"Entry Time: {self.entry_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if DRY_RUN:
            print(f"🔶 DRY RUN: Orders NOT placed (simulation only)")
            logger.info("DRY RUN: Orders NOT placed (simulation only)")
        else:
            # Place actual orders
            try:
                # Place Call order
                call_order = self._place_order(broker, call_symbol, "BUY", 1)  # Assuming 1 lot
                if call_order and call_order.get('s') == 'ok':
                    self.call_order_id = call_order.get('id', None)
                    print(f"✅ Call Order Placed: Order ID {self.call_order_id}")
                    logger.info(f"Call Order Placed: Order ID {self.call_order_id}")
                else:
                    logger.error(f"Call Order Failed: {call_order}")
                
                # Place Put order
                put_order = self._place_order(broker, put_symbol, "BUY", 1)  # Assuming 1 lot
                if put_order and put_order.get('s') == 'ok':
                    self.put_order_id = put_order.get('id', None)
                    print(f"✅ Put Order Placed: Order ID {self.put_order_id}")
                    logger.info(f"Put Order Placed: Order ID {self.put_order_id}")
                else:
                    logger.error(f"Put Order Failed: {put_order}")
                    
            except Exception as e:
                print(f"❌ Error placing orders: {e}")
                logger.error(f"Error placing orders: {e}")
        
        print(f"{'='*80}\n")
    
    def _place_order(self, broker, symbol, side, qty):
        """Place order via broker API"""
        if not broker:
            return None
        
        try:
            return broker.place_order(symbol, side, qty, order_type="MARKET", price=0)
        except Exception as e:
            logger.error(f"Error placing order for {symbol}: {e}")
            return None
    
    def check_exit(self, current_call_price, current_put_price):
        """Check if exit condition is met"""
        if not self.is_position_open:
            return False
        
        current_total = current_call_price + current_put_price
        profit = current_total - self.entry_total_price
        
        if profit >= TARGET_PROFIT_POINTS:
            # Note: fyers_model will be passed from monitor_exit_condition
            return True  # Exit will be handled by monitor_exit_condition
        return False
    
    def exit_trade(self, exit_call_price, exit_put_price, profit, broker=None):
        """Record trade exit and place exit orders if not in dry run"""
        # Prevent duplicate exits
        if not self.is_position_open:
            return
            
        exit_total = exit_call_price + exit_put_price
        exit_time = datetime.now()
        duration = exit_time - self.entry_time
        
        mode_text = "🔶 [DRY RUN] " if DRY_RUN else "🔴 [LIVE] "
        
        print(f"\n{'='*80}")
        print(f"{mode_text}TRADE EXITED - TARGET PROFIT ACHIEVED")
        print(f"{'='*80}")
        print(f"Exit Call Price: ₹{exit_call_price:.2f}")
        print(f"Exit Put Price: ₹{exit_put_price:.2f}")
        print(f"Exit Total Price: ₹{exit_total:.2f}")
        print(f"Entry Total Price: ₹{self.entry_total_price:.2f}")
        print(f"Profit: ₹{profit:.2f} ({profit:.2f} points)")
        print(f"Entry Time: {self.entry_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Exit Time: {exit_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Duration: {duration}")
        
        # Log trade exit
        logger.info(f"{mode_text}TRADE EXITED - TARGET PROFIT ACHIEVED")
        logger.info(f"Exit Call Price: ₹{exit_call_price:.2f} | Exit Put Price: ₹{exit_put_price:.2f}")
        logger.info(f"Exit Total: ₹{exit_total:.2f} | Entry Total: ₹{self.entry_total_price:.2f}")
        logger.info(f"Profit: ₹{profit:.2f} ({profit:.2f} points)")
        logger.info(f"Duration: {duration}")
        
        if DRY_RUN:
            print(f"🔶 DRY RUN: Exit orders NOT placed (simulation only)")
            logger.info("DRY RUN: Exit orders NOT placed (simulation only)")
        else:
            # Place exit orders
            try:
                # Exit Call order
                if self.call_order_id:
                    call_exit_order = self._place_order(broker, self.call_symbol, "SELL", 1)
                    if call_exit_order and call_exit_order.get('s') == 'ok':
                        print(f"✅ Call Exit Order Placed: Order ID {call_exit_order.get('id', None)}")
                        logger.info(f"Call Exit Order Placed: Order ID {call_exit_order.get('id', None)}")
                    else:
                        logger.error(f"Call Exit Order Failed: {call_exit_order}")
                
                # Exit Put order
                if self.put_order_id:
                    put_exit_order = self._place_order(broker, self.put_symbol, "SELL", 1)
                    if put_exit_order and put_exit_order.get('s') == 'ok':
                        print(f"✅ Put Exit Order Placed: Order ID {put_exit_order.get('id', None)}")
                        logger.info(f"Put Exit Order Placed: Order ID {put_exit_order.get('id', None)}")
                    else:
                        logger.error(f"Put Exit Order Failed: {put_exit_order}")
                        
            except Exception as e:
                print(f"❌ Error placing exit orders: {e}")
                logger.error(f"Error placing exit orders: {e}")
        
        print(f"{'='*80}\n")
        
        self.is_position_open = False
        self.trade_completed = True  # Mark trade as completed
        
        # Log trade completion
        if logger:
            logger.info("=" * 100)
            logger.info("TRADE COMPLETED - NO MORE TRADES WILL BE ENTERED TODAY")
            logger.info("=" * 100)
            logger.info("Continuing to log data for the rest of the day...")
            logger.info("=" * 100)

def calculate_otm_volumes(all_option_data, atm_strike):
    """
    Calculate total volume of OTM call and put strike prices
    Returns: (otm_call_volume, otm_put_volume)
    """
    global logger
    otm_call_volume = 0
    otm_put_volume = 0
    
    for option in all_option_data:
        strike = option.get('Strike', 0)
        volume = option.get('Volume', 0)
        option_type = option.get('Option_Type', '')
        
        if option_type == 'CE' and strike > atm_strike:
            # OTM Call (strikes above ATM)
            otm_call_volume += volume
        elif option_type == 'PE' and strike < atm_strike:
            # OTM Put (strikes below ATM)
            otm_put_volume += volume
    
    # Log volume calculation (if logger is available)
    if logger:
        logger.info(f"Volume Analysis - ATM: {atm_strike}, OTM Call Vol: {otm_call_volume:,.0f}, OTM Put Vol: {otm_put_volume:,.0f}")
    
    return otm_call_volume, otm_put_volume

def find_strike_with_premium_range(all_option_data, option_type, min_premium, max_premium):
    """
    Find strike price with premium (LTP) in the specified range
    Returns: (strike, premium, symbol) or (None, None, None) if not found
    """
    for option in all_option_data:
        opt_type = option.get('Option_Type', '')
        premium = option.get('LTP', 0)
        
        if opt_type == option_type and min_premium <= premium <= max_premium:
            strike = option.get('Strike', 0)
            symbol = option.get('Symbol', '')
            return strike, premium, symbol
    
    return None, None, None

def check_entry_condition(otm_call_volume, otm_put_volume):
    """
    Check if entry condition is met:
    OTM call volume >= (2 * OTM put volume) OR OTM put volume >= (2 * OTM call volume)
    Returns: True if condition met, False otherwise
    """
    if otm_call_volume >= (VOLUME_RATIO_THRESHOLD * otm_put_volume):
        return True, "CALL_VOLUME_DOMINANT"
    elif otm_put_volume >= (VOLUME_RATIO_THRESHOLD * otm_call_volume):
        return True, "PUT_VOLUME_DOMINANT"
    else:
        return False, "NO_CONDITION"

def check_existing_positions(broker):
    """
    Check for existing open positions using broker abstraction
    Returns: (call_symbol, put_symbol, call_strike, put_strike, call_price, put_price) or None
    """
    try:
        # Get positions from broker API
        positions_response = broker.get_positions()
        
        # Handle different broker response formats
        if isinstance(broker, FyersBroker):
            if positions_response.get('s') != 'ok':
                logger.warning(f"Failed to fetch positions: {positions_response.get('message', 'Unknown error')}")
                return None
            positions_data = positions_response.get('netPositions', [])
        elif isinstance(broker, AngelBroker):
            if not positions_response.get('status') or not positions_response.get('data'):
                logger.warning(f"Failed to fetch positions: {positions_response.get('message', 'Unknown error')}")
                return None
            positions_data = positions_response.get('data', [])
        else:
            return None
        
        if not positions_data or len(positions_data) == 0:
            return None
        
        # Filter for Nifty options (CE and PE)
        call_position = None
        put_position = None
        
        for pos in positions_data:
            symbol = pos.get('symbol', '')
            qty = pos.get('netQty', 0)
            
            # Only consider long positions (qty > 0) for Nifty options
            if 'NIFTY' in symbol and 'NSE' in symbol and qty > 0:
                if 'CE' in symbol:
                    call_position = pos
                elif 'PE' in symbol:
                    put_position = pos
        
        # Need both call and put positions
        if not call_position or not put_position:
            return None
        
        # Extract strike prices from symbols
        # Format: NSE:NIFTY2611326200CE -> strike is 26200
        call_symbol = call_position.get('symbol', '')
        put_symbol = put_position.get('symbol', '')
        
        # Extract strike from symbol (last digits before CE/PE)
        try:
            import re
            # Extract all digit sequences
            call_digits = re.findall(r'\d+', call_symbol.replace('NSE:NIFTY', '').replace('CE', ''))
            put_digits = re.findall(r'\d+', put_symbol.replace('NSE:NIFTY', '').replace('PE', ''))
            
            # Strike is typically the last significant number sequence
            # For NSE:NIFTY2611326200CE: ['26113', '26200'] -> strike is 26200
            if len(call_digits) >= 2:
                call_strike = int(call_digits[-1])  # Last digit sequence is strike
            else:
                # Fallback: try to extract from end
                call_strike_str = call_symbol.replace('NSE:NIFTY', '').replace('CE', '')
                call_strike = int(call_strike_str[-5:]) if len(call_strike_str) >= 5 else None
            
            if len(put_digits) >= 2:
                put_strike = int(put_digits[-1])  # Last digit sequence is strike
            else:
                # Fallback: try to extract from end
                put_strike_str = put_symbol.replace('NSE:NIFTY', '').replace('PE', '')
                put_strike = int(put_strike_str[-5:]) if len(put_strike_str) >= 5 else None
            
            if not call_strike or not put_strike:
                logger.warning(f"Could not extract strike prices from symbols: {call_symbol}, {put_symbol}")
                return None
            
        except (ValueError, IndexError) as e:
            logger.warning(f"Error extracting strike prices: {e}")
            return None
        
        # Get current prices (LTP) from positions
        call_price = call_position.get('ltp', 0) or call_position.get('avgPrice', 0)
        put_price = put_position.get('ltp', 0) or put_position.get('avgPrice', 0)
        
        # If LTP is 0, try to get from quotes API
        if call_price == 0 or put_price == 0:
            call_quote = broker.get_option_quote(call_symbol)
            put_quote = broker.get_option_quote(put_symbol)
            
            if isinstance(broker, FyersBroker):
                if call_quote:
                    call_price = call_quote.get('lp', 0) or call_price
                if put_quote:
                    put_price = put_quote.get('lp', 0) or put_price
            elif isinstance(broker, AngelBroker):
                if call_quote:
                    call_price = float(call_quote.get('ltp', 0)) or call_price
                if put_quote:
                    put_price = float(put_quote.get('ltp', 0)) or put_price
        
        logger.info(f"Found existing positions:")
        logger.info(f"  Call: {call_symbol} (Strike: {call_strike}) @ ₹{call_price:.2f}")
        logger.info(f"  Put: {put_symbol} (Strike: {put_strike}) @ ₹{put_price:.2f}")
        
        return (call_symbol, put_symbol, call_strike, put_strike, call_price, put_price)
        
    except Exception as e:
        logger.error(f"Error checking existing positions: {e}")
        import traceback
        traceback.print_exc()
        return None

def find_entry_strikes(all_option_data, nifty_spot):
    """
    Find call and put strike prices with premiums between 43-57
    and total premium <= 100
    Prefers strikes closer to Nifty spot price
    
    Returns: (call_strike, put_strike, call_price, put_price, call_symbol, put_symbol) or None
    """
    # Find all call strikes in range
    call_candidates = []
    for option in all_option_data:
        if option.get('Option_Type') == 'CE':
            premium = option.get('LTP', 0)
            strike = option.get('Strike', 0)
            if MIN_PREMIUM <= premium <= MAX_PREMIUM:
                # Calculate distance from spot (prefer strikes closer to spot)
                # For calls, we want strikes >= spot, so distance = strike - spot
                distance_from_spot = abs(strike - nifty_spot)
                call_candidates.append({
                    'strike': strike,
                    'premium': premium,
                    'symbol': option.get('Symbol', ''),
                    'distance': distance_from_spot
                })
    
    # Find all put strikes in range
    put_candidates = []
    for option in all_option_data:
        if option.get('Option_Type') == 'PE':
            premium = option.get('LTP', 0)
            strike = option.get('Strike', 0)
            if MIN_PREMIUM <= premium <= MAX_PREMIUM:
                # Calculate distance from spot (prefer strikes closer to spot)
                # For puts, we want strikes <= spot, so distance = spot - strike
                distance_from_spot = abs(strike - nifty_spot)
                put_candidates.append({
                    'strike': strike,
                    'premium': premium,
                    'symbol': option.get('Symbol', ''),
                    'distance': distance_from_spot
                })
    
    # Sort candidates by distance from spot (closer first)
    call_candidates.sort(key=lambda x: x['distance'])
    put_candidates.sort(key=lambda x: x['distance'])
    
    # Find best combination where total <= 100
    # Try all combinations and find the one with minimum total distance from spot
    best_combination = None
    best_total_distance = float('inf')
    
    for call in call_candidates:
        for put in put_candidates:
            total_premium = call['premium'] + put['premium']
            if total_premium <= MAX_TOTAL_PREMIUM:
                total_distance = call['distance'] + put['distance']
                # Prefer combination with minimum total distance from spot
                if total_distance < best_total_distance:
                    best_total_distance = total_distance
                    best_combination = (
                        call['strike'], put['strike'],
                        call['premium'], put['premium'],
                        call['symbol'], put['symbol']
                    )
    
    return best_combination

def monitor_exit_condition(broker, algo_trade, handler=None, check_interval=None):
    """
    Monitor exit condition using real-time data
    Uses WebSocket data if available, otherwise falls back to API polling
    When using API polling, checks every API_POLLING_INTERVAL seconds (default: 1 second)
    """
    import time
    
    # Use API_POLLING_INTERVAL if WebSocket is not available or disabled
    if check_interval is None:
        check_interval = API_POLLING_INTERVAL if not handler else 0.1  # Small delay for WebSocket
    
    print(f"\n🔄 Monitoring exit condition...")
    print(f"   Target Profit: {TARGET_PROFIT_POINTS} points")
    print(f"   Entry Total: ₹{algo_trade.entry_total_price:.2f}")
    print(f"   Target Exit: ₹{algo_trade.entry_total_price + TARGET_PROFIT_POINTS:.2f}")
    if handler:
        print(f"   Using WebSocket for real-time updates")
    else:
        print(f"   Using API polling (checking every {check_interval} seconds)")
    print()
    
    # Track start time for timeout (10-15 seconds as requested)
    start_time = time.time()
    max_wait_time = 15  # Maximum wait time in seconds
    
    while algo_trade.is_position_open:
        try:
            # Check timeout (optional - only if you want to limit monitoring time)
            elapsed_time = time.time() - start_time
            if elapsed_time > max_wait_time:
                print(f"⚠️  Monitoring timeout after {max_wait_time} seconds. Trade still open.")
                # Continue monitoring anyway, just log the timeout
            
            current_call_price = 0
            current_put_price = 0
            
            # Try WebSocket data first if available
            if handler:
                call_rt = handler.get_realtime_ohlcv(algo_trade.call_symbol)
                put_rt = handler.get_realtime_ohlcv(algo_trade.put_symbol)
                
                if call_rt:
                    current_call_price = call_rt.get('LTP', 0)
                if put_rt:
                    current_put_price = put_rt.get('LTP', 0)
            
            # Fallback to API if WebSocket data not available or disabled
            if current_call_price == 0 or current_put_price == 0:
                call_data = get_option_ohlcv_realtime(broker, algo_trade.call_symbol)
                put_data = get_option_ohlcv_realtime(broker, algo_trade.put_symbol)
                
                if call_data:
                    current_call_price = call_data.get('LTP', 0)
                if put_data:
                    current_put_price = put_data.get('LTP', 0)
            
            if current_call_price > 0 and current_put_price > 0:
                current_total = current_call_price + current_put_price
                current_profit = current_total - algo_trade.entry_total_price
                
                print(f"📊 Current: Call ({algo_trade.entry_call_strike})=₹{current_call_price:.2f}, Put ({algo_trade.entry_put_strike})=₹{current_put_price:.2f}, Total=₹{current_total:.2f}, Profit=₹{current_profit:.2f} (Target: ₹{TARGET_PROFIT_POINTS:.2f})")
                
                # Check exit condition
                if algo_trade.check_exit(current_call_price, current_put_price):
                    # Exit trade
                    profit = current_total - algo_trade.entry_total_price
                    algo_trade.exit_trade(current_call_price, current_put_price, profit, broker)
                    break
            else:
                print("⚠️  Could not fetch current prices, retrying...")
            
            # Always sleep when using API polling (handler is None)
            # For WebSocket, use a small delay to prevent tight loop
            if not handler:
                time.sleep(check_interval)
            else:
                time.sleep(0.1)  # Small delay for WebSocket to allow message processing
            
        except KeyboardInterrupt:
            print("\n⚠️  Monitoring stopped by user")
            break
        except Exception as e:
            print(f"❌ Error monitoring: {e}")
            # Sleep on error to avoid tight error loop
            time.sleep(check_interval if not handler else 0.1)

# =========================================================================
# 📡 WEBSOCKET FUNCTIONS
# =========================================================================

class FyersWebSocketHandler:
    def __init__(self, access_token, symbols):
        self.access_token = access_token
        self.symbols = symbols
        self.data_received = []
        self.realtime_data = {}  # Store real-time OHLCV data by symbol
        
    def on_message(self, message):
        """Handle incoming WebSocket messages - process real-time OHLCV data"""
        try:
            data = json.loads(message)
            
            # Process real-time data updates
            if isinstance(data, dict):
                # Check if it's a symbol update
                if 'd' in data:
                    for item in data.get('d', []):
                        symbol = item.get('n', '')  # Symbol name
                        if symbol:
                            # Extract OHLCV from real-time update
                            v = item.get('v', {})
                            self.realtime_data[symbol] = {
                                'Symbol': symbol,
                                'LTP': v.get('lp', 0),
                                'Open': v.get('open_price', 0),
                                'High': v.get('high_price', 0),
                                'Low': v.get('low_price', 0),
                                'Close': v.get('lp', 0),  # Use LTP as close
                                'Volume': v.get('volume', 0),
                                'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            }
                            
                            # Print real-time update
                            rt_data = self.realtime_data[symbol]
                            if rt_data['LTP'] > 0:  # Only print if we have valid data
                                print(f"📊 {symbol}: LTP={rt_data['LTP']:.2f}, O={rt_data['Open']:.2f}, H={rt_data['High']:.2f}, L={rt_data['Low']:.2f}, V={rt_data['Volume']}")
            
            self.data_received.append(data)
        except Exception as e:
            print(f"Error parsing WebSocket message: {e}")
            print(f"Raw message: {message}")
    
    def on_error(self, error):
        """Handle WebSocket errors"""
        print(f"❌ WebSocket Error: {error}")
    
    def on_close(self, close_message):
        """Handle WebSocket close"""
        print(f"🔌 WebSocket Connection Closed: {close_message}")
    
    def on_open(self):
        """Handle WebSocket open - subscribe to symbols"""
        print(f"✅ WebSocket Connected! Subscribing to {len(self.symbols)} symbols...")
        print(f"📡 Real-time data streaming started...")
        print(f"Symbols: {', '.join(self.symbols[:5])}..." if len(self.symbols) > 5 else f"Symbols: {', '.join(self.symbols)}")
    
    def get_realtime_ohlcv(self, symbol):
        """Get latest real-time OHLCV data for a symbol"""
        return self.realtime_data.get(symbol, None)

def connect_websocket(access_token, symbols):
    """Connect to Fyers WebSocket for real-time data"""
    if not FYERS_WEBSOCKET_AVAILABLE:
        print("❌ WebSocket functionality is not available. Please install fyers-apiv3 package.")
        return None, None
    
    try:
        handler = FyersWebSocketHandler(access_token, symbols)
        
        fyers_ws = data_ws.FyersDataSocket(
            access_token=access_token,
            write_to_file=False,
            log_path="",
            on_connect=handler.on_open,
            on_close=handler.on_close,
            on_error=handler.on_error,
            on_message=handler.on_message
        )
        
        # Connect to WebSocket
        fyers_ws.connect()
        
        # Subscribe to symbols
        fyers_ws.subscribe(symbols=symbols, data_type="symbolUpdate")
        
        print("🔄 WebSocket is running. Press Ctrl+C to stop...")
        
        # Keep the connection alive
        fyers_ws.keep_running()
        
        return fyers_ws, handler
        
    except Exception as e:
        print(f"❌ Error connecting to WebSocket: {e}")
        import traceback
        traceback.print_exc()
        return None, None

# =========================================================================
# 🕐 MARKET HOURS UTILITIES
# =========================================================================

def is_market_open():
    """Check if NSE market is currently open (9:15 AM - 3:30 PM IST)"""
    try:
        import pytz
        # Get current time in IST
        ist = pytz.timezone('Asia/Kolkata')
        now = datetime.now(ist)
    except ImportError:
        # Fallback: assume system time is IST (or adjust manually)
        now = datetime.now()
        print("⚠️  pytz not installed. Using system time (assume IST). Install with: pip install pytz")
    
    current_time = now.time()
    
    # Market hours: 9:15 AM to 3:30 PM IST
    market_open = datetime.strptime('09:15:00', '%H:%M:%S').time()
    market_close = datetime.strptime('15:30:00', '%H:%M:%S').time()
    
    # Check if it's a weekday (Monday=0, Sunday=6)
    is_weekday = now.weekday() < 5
    
    return is_weekday and market_open <= current_time <= market_close

def wait_for_market_open():
    """Wait until market opens (9:15 AM IST)"""
    try:
        import pytz
        ist = pytz.timezone('Asia/Kolkata')
        now = datetime.now(ist)
        timezone_name = "IST"
        use_timezone = True
    except ImportError:
        # Fallback: assume system time is IST
        now = datetime.now()
        timezone_name = "System Time (assume IST)"
        use_timezone = False
        print("⚠️  pytz not installed. Using system time. Install with: pip install pytz")
    
    current_time = now.time()
    
    # Market opens at 9:15 AM IST
    market_open_time = datetime.strptime('09:15:00', '%H:%M:%S').time()
    
    # If market is already open, return immediately
    if is_market_open():
        print(f"✅ Market is already open! (Current time: {now.strftime('%H:%M:%S')} {timezone_name})")
        return
    
    # Calculate time until market opens
    if use_timezone:
        # Make market_open_datetime timezone-aware
        market_open_datetime = ist.localize(datetime.combine(now.date(), market_open_time))
    else:
        # Use naive datetime
        market_open_datetime = datetime.combine(now.date(), market_open_time)
    
    # If current time is after market close, wait until next day
    if current_time > datetime.strptime('15:30:00', '%H:%M:%S').time():
        days_to_add = 1
        # Skip weekends
        while True:
            next_date = now.date() + timedelta(days=days_to_add)
            if next_date.weekday() < 5:  # Monday=0, Friday=4
                break
            days_to_add += 1
        
        # Create new datetime for next trading day
        if use_timezone:
            market_open_datetime = ist.localize(datetime.combine(next_date, market_open_time))
        else:
            market_open_datetime = datetime.combine(next_date, market_open_time)
    
    wait_seconds = (market_open_datetime - now).total_seconds()
    
    if wait_seconds > 0:
        wait_minutes = wait_seconds / 60
        print(f"\n⏳ Market opens at 9:15 AM IST")
        print(f"   Current time: {now.strftime('%H:%M:%S')} ({timezone_name})")
        print(f"   Waiting {wait_minutes:.1f} minutes until market opens...")
        print(f"   (Press Ctrl+C to exit)\n")
        
        # Wait in 10-second intervals, checking every 10 seconds
        while wait_seconds > 0:
            sleep_time = min(10, wait_seconds)  # Check every 10 seconds
            time.sleep(sleep_time)
            wait_seconds -= sleep_time
            
            # Update remaining time
            remaining_minutes = wait_seconds / 60
            if wait_seconds > 60:
                print(f"   ⏳ {remaining_minutes:.1f} minutes remaining...", end='\r')
            elif wait_seconds > 0:
                print(f"   ⏳ {int(wait_seconds)} seconds remaining...", end='\r')
        
        print(f"\n✅ Market is now open! Starting data collection...\n")
    else:
        print("✅ Market is already open!")

# =========================================================================
# 🚀 MAIN EXECUTION
# =========================================================================

def main():
    global logger, db_conn
    
    # Initialize logger and database FIRST
    logger = setup_logging()
    db_conn = setup_database()
    
    # Check market hours and initialize broker early (to load contract master before market opens)
    print("="*80)
    print("🕐 CHECKING MARKET HOURS")
    print("="*80)
    
    market_is_open = is_market_open()
    
    print("="*80)
    broker_name = BROKER.upper()
    print(f"🚀 NIFTY OPTIONS ALGO - {broker_name} API CONNECTION")
    print("="*80)
    
    # Display configuration
    print(f"\n📋 Configuration:")
    print(f"   MAX_TOTAL_PREMIUM: ₹{MAX_TOTAL_PREMIUM:.2f}")
    if NIFTY_PREOPEN_PRICE:
        print(f"   NIFTY_PREOPEN_PRICE: ₹{NIFTY_PREOPEN_PRICE:.2f} (configured)")
    else:
        print(f"   NIFTY_PREOPEN_PRICE: Not set (will use API)")
    print()
    
    # Initialize Broker early (before market opens) to load contract master
    try:
        broker = get_broker()
        broker.initialize()
        
        # If using Angel broker, contract master is already loaded during initialize()
        # This allows us to use it even before market opens for pre-open data
        if isinstance(broker, AngelBroker) and broker.contract_master:
            print(f"✅ Contract master file loaded ({len(broker.contract_master)} contracts)")
            print("   Ready to fetch tokens for symbols (can use pre-open data)")
        
        # Verify connection
        profile = broker.get_profile()
        if isinstance(broker, FyersBroker):
            if profile.get("code") == 200 or profile.get('s') == 'ok':
                user_name = profile.get('data', {}).get('name', 'User')
                print(f"✅ {broker_name} API Login Successful. Welcome, {user_name}")
            else:
                print(f"❌ {broker_name} API Login Failed: {profile.get('message', 'Unknown error')}")
                return
        elif isinstance(broker, AngelBroker):
            if profile.get('status') and profile.get('data'):
                user_name = profile.get('data', {}).get('name', 'User')
                print(f"✅ {broker_name} API Login Successful. Welcome, {user_name}")
            else:
                print(f"❌ {broker_name} API Login Failed: {profile.get('message', 'Unknown error')}")
                return
                
    except ImportError as e:
        print(f"❌ {broker_name} API Package Not Installed: {e}")
        print(f"Please install: pip install {'fyers-apiv3' if BROKER == 'fyers' else 'smartapi-python pyotp'}")
        return
    except ValueError as e:
        print(f"❌ Configuration Error: {e}")
        print(f"Please check your .env file and set the required credentials for {broker_name}")
        return
    except Exception as e:
        print(f"❌ {broker_name} API Initialization Error: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Initialize algo trade object
    algo_trade = AlgoTrade()
    
    # STEP 0: Check for existing positions first
    print("\n" + "="*80)
    print("STEP 0: Checking for Existing Positions")
    print("="*80)
    
    existing_positions = check_existing_positions(broker)
    if existing_positions:
        call_symbol, put_symbol, call_strike, put_strike, call_price, put_price = existing_positions
        
        print(f"\n✅ Found existing open positions:")
        print(f"   Call: {call_symbol} (Strike: {call_strike}) @ ₹{call_price:.2f}")
        print(f"   Put: {put_symbol} (Strike: {put_strike}) @ ₹{put_price:.2f}")
        print(f"   Total Entry Price: ₹{call_price + put_price:.2f}")
        print(f"\n🔄 Restoring trade state and starting exit monitoring...")
        
        # Restore trade state
        algo_trade.restore_from_positions(call_symbol, put_symbol, call_strike, put_strike, call_price, put_price)
        
        # Log restoration
        logger.info("="*100)
        logger.info("EXISTING POSITIONS DETECTED - RESTORING TRADE STATE")
        logger.info(f"Call: {call_symbol} (Strike: {call_strike}) @ ₹{call_price:.2f}")
        logger.info(f"Put: {put_symbol} (Strike: {put_strike}) @ ₹{put_price:.2f}")
        logger.info(f"Total Entry: ₹{algo_trade.entry_total_price:.2f}")
        logger.info("="*100)
        
        # Skip entry logic and go straight to monitoring
        # Get Nifty spot for WebSocket subscription
        nifty_spot = get_nifty_spot_price(broker)
        if nifty_spot is None:
            print("⚠️  Warning: Failed to fetch Nifty spot, but continuing with position monitoring...")
            nifty_spot = 0  # Will be updated from WebSocket
        
        # Set up WebSocket for monitoring (if enabled)
        expiry_date = get_nearest_expiry()
        all_strikes = [call_strike, put_strike]  # Only subscribe to our positions
        
        # Create symbols list for WebSocket
        option_symbols = [call_symbol, put_symbol]
        
        # Connect WebSocket if enabled and available
        fyers_ws = None
        handler = None
        use_websocket = USE_WEBSOCKET and FYERS_WEBSOCKET_AVAILABLE and isinstance(broker, FyersBroker)
        
        if use_websocket:
            print(f"\n🔄 Connecting to WebSocket for real-time monitoring...")
            fyers_ws, handler = broker.connect_websocket(option_symbols, FyersWebSocketHandler(broker.clean_token, option_symbols))
            if not fyers_ws or not handler:
                print(f"⚠️  WebSocket connection failed. Falling back to API polling (every {API_POLLING_INTERVAL} seconds)")
                handler = None
        else:
            if not USE_WEBSOCKET:
                print(f"ℹ️  WebSocket disabled. Using API polling (every {API_POLLING_INTERVAL} seconds)")
            elif not isinstance(broker, FyersBroker):
                print(f"ℹ️  WebSocket not available for {BROKER.upper()}. Using API polling (every {API_POLLING_INTERVAL} seconds)")
            else:
                print(f"ℹ️  WebSocket not available. Using API polling (every {API_POLLING_INTERVAL} seconds)")
        
        # Start monitoring exit condition
        monitor_exit_condition(broker, algo_trade, handler, check_interval=API_POLLING_INTERVAL)
        
        # After exit, continue with data logging if enabled
        if CONTINUE_LOGGING_AFTER_TRADE:
            print(f"\n📊 Continuing data logging after trade completion...")
            # Continue with normal data logging flow
            # (This will be handled by the main loop below)
        
        return
    
    print("ℹ️  No existing positions found. Starting fresh...")
    logger.info("No existing positions found. Starting fresh trading session.")
    
    # Wait for market to open if not already open
    if not market_is_open:
        wait_for_market_open()
        market_is_open = is_market_open()  # Re-check after waiting
    
    print("\n" + "="*80)
    print("STEP 1: Downloading Nifty Spot Price")
    print("="*80)
    
    # If pre-open price is set and market is not open, use it immediately
    if NIFTY_PREOPEN_PRICE and not market_is_open:
        print(f"ℹ️  Market not open yet. Using pre-open price from config: ₹{NIFTY_PREOPEN_PRICE:.2f}")
        nifty_spot = NIFTY_PREOPEN_PRICE
        logger.info(f"Using NIFTY_PREOPEN_PRICE from config (market not open): ₹{NIFTY_PREOPEN_PRICE:.2f}")
    else:
        # Try to get Nifty spot price from API
        nifty_spot = get_nifty_spot_price(broker)
        if nifty_spot is None:
            if not market_is_open:
                print("⚠️  Market not open yet. Trying to get pre-open data from API...")
                # Retry immediately without delay for faster execution
                nifty_spot = get_nifty_spot_price(broker)
            
            # If still None, try using pre-open price from config
            if nifty_spot is None and NIFTY_PREOPEN_PRICE:
                print(f"⚠️  API failed to fetch Nifty spot. Using pre-open price from config: ₹{NIFTY_PREOPEN_PRICE:.2f}")
                nifty_spot = NIFTY_PREOPEN_PRICE
                logger.info(f"Using NIFTY_PREOPEN_PRICE from config (API failed): ₹{NIFTY_PREOPEN_PRICE:.2f}")
            elif nifty_spot is None:
                print("❌ Failed to fetch Nifty spot price.")
                print("💡 Tip: Set NIFTY_PREOPEN_PRICE in .env file before 09:15 to use pre-open price")
                print("   Example: NIFTY_PREOPEN_PRICE=25450.00")
                print("   Exiting...")
                return
    
    print(f"📊 Nifty Spot Price: ₹{nifty_spot:.2f}")
    
    # Log Nifty spot clearly
    logger.info("-" * 100)
    logger.info(f"NIFTY SPOT PRICE: ₹{nifty_spot:.2f}")
    logger.info("-" * 100)
    
    print("\n" + "="*80)
    print("STEP 2: Calculating ATM and Strike Prices")
    print("="*80)
    
    # Calculate strikes (use fast mode for quick entry)
    atm_strike, strikes_above, strikes_below, all_strikes = get_strike_prices(nifty_spot, fast_mode=FAST_ENTRY_MODE)
    
    print(f"🎯 ATM Strike: {atm_strike}")
    print(f"📈 Strikes Above ATM: {strikes_above}")
    print(f"📉 Strikes Below ATM: {strikes_below}")
    print(f"📋 All Strikes: {all_strikes}")
    
    # Log ATM calculation
    logger.info(f"ATM CALCULATION: Spot={nifty_spot:.2f} → ATM Strike={atm_strike}")
    logger.info(f"Strikes Above ATM: {strikes_above}")
    logger.info(f"Strikes Below ATM: {strikes_below}")
    
    print("\n" + "="*80)
    print("STEP 3: Getting Option Expiry Date")
    print("="*80)
    
    # Get nearest expiry (Tuesday for Nifty)
    expiry_date = get_nearest_expiry()
    # Format expiry for symbol: YYMDD (e.g., 26113 for Jan 13, 2026) - month without leading zero
    year = expiry_date.strftime('%y')
    month = str(expiry_date.month)  # No leading zero
    day = expiry_date.strftime('%d')
    expiry_symbol_format = f"{year}{month}{day}"
    print(f"📅 Nearest Expiry Date: {expiry_date.strftime('%d-%b-%Y')} (Tuesday)")
    print(f"📅 Symbol Format: NSE:NIFTY{expiry_symbol_format}XXXXXCE/PE (YYMDD format - month without leading zero)")
    print(f"   Example: NSE:NIFTY{expiry_symbol_format}{atm_strike}CE")
    print()
    print("ℹ️  Note: If you see zeros, it could mean:")
    print("   - Market is closed (NSE hours: 9:15 AM - 3:30 PM IST)")
    print("   - Option contracts don't exist for this expiry")
    print("   - Symbol format might need adjustment")
    print("   - Trying historical data API as fallback...")
    
    print("\n" + "="*80)
    print("STEP 4: Downloading OHLCV Data for All Strikes")
    print("="*80)
    
    # Download data for all strikes (both CE and PE)
    all_option_data = []
    option_symbols = []
    
    # Fast entry mode: Use smart expanding search with async concurrent fetching
    if FAST_ENTRY_MODE and USE_ASYNC_FETCHING:
        # Smart fetching: Expands outward until finding strikes with premiums in range
        # Use async version for concurrent API calls (much faster)
        all_option_data, option_symbols, found_premiums = asyncio.run(
            fetch_strikes_smart_async(broker, expiry_date, atm_strike, nifty_spot)
        )
    elif FAST_ENTRY_MODE:
        # Fallback to synchronous version if async is disabled
        all_option_data, option_symbols, found_premiums = fetch_strikes_smart(broker, expiry_date, atm_strike, nifty_spot)
    else:
        # Normal mode: Fetch all strikes at once
        print("📊 Fetching initial real-time quotes...")
        print("⚠️  Note: If market is closed, you'll see zeros. Use WebSocket for live data when market opens.")
        print()
        
        # Debug: Print expiry and symbol format based on broker
        print(f"🔍 Debug: Expiry Date = {expiry_date.strftime('%d-%b-%Y')} (Tuesday)")
        if isinstance(broker, AngelBroker):
            day_debug = expiry_date.strftime('%d')
            month_debug = expiry_date.strftime('%b').upper()
            year_debug = expiry_date.strftime('%y')  # 2-digit year
            expiry_formatted_debug = f"{day_debug}{month_debug}{year_debug}"  # DDMMMYY format
            print(f"🔍 Debug: Symbol Format = NSE:NIFTY{expiry_formatted_debug}XXXXXCE/PE (DDMMMYY format for Angel - matches JSON)")
            print(f"🔍 Debug: Example Symbol (ATM) = NSE:NIFTY{expiry_formatted_debug}{atm_strike}CE")
        else:
            year_debug = expiry_date.strftime('%y')
            month_debug = str(expiry_date.month)  # No leading zero
            day_debug = expiry_date.strftime('%d')
            expiry_formatted_debug = f"{year_debug}{month_debug}{day_debug}"
            print(f"🔍 Debug: Symbol Format = NSE:NIFTY{expiry_formatted_debug}XXXXXCE/PE (YYMDD format for Fyers)")
            print(f"🔍 Debug: Example Symbol (ATM) = NSE:NIFTY{expiry_formatted_debug}{atm_strike}CE")
        print(f"ℹ️  Note: Far OTM strikes may have no data if contracts are inactive")
        print()
        
        # First, test ATM strike to verify symbol format is correct
        test_ce_symbol = get_option_symbol(expiry_date, atm_strike, "CE", broker)
        test_ce_data = get_option_ohlcv_realtime(broker, test_ce_symbol)
        if test_ce_data and (test_ce_data['LTP'] > 0 or test_ce_data['Volume'] > 0):
            print(f"✅ Symbol format verified: {test_ce_symbol} has data")
            print()
        elif test_ce_data:
            print(f"⚠️  Warning: {test_ce_symbol} returned data but LTP=0, Volume=0")
            print(f"   This might indicate symbol format issue or contract inactive")
            print()
        else:
            print(f"❌ Error: {test_ce_symbol} failed to fetch - checking symbol format...")
            broker_name = "Angel Smart API" if isinstance(broker, AngelBroker) else "Fyers API"
            print(f"   Please verify expiry date and symbol format in {broker_name} docs")
            print()
        
        # Normal mode: Fetch all strikes
        for strike in all_strikes:
            # Call Option
            ce_symbol = get_option_symbol(expiry_date, strike, "CE")
            option_symbols.append(ce_symbol)
            ce_data = get_option_ohlcv_realtime(broker, ce_symbol)
            if ce_data:
                ce_data['Strike'] = strike
                ce_data['Option_Type'] = 'CE'
                all_option_data.append(ce_data)
                # Skip verbose prints in fast entry mode
                if not FAST_ENTRY_MODE:
                    if ce_data['LTP'] > 0 or ce_data['Volume'] > 0:
                        print(f"✅ {ce_symbol}: LTP={ce_data['LTP']:.2f}, O={ce_data['Open']:.2f}, H={ce_data['High']:.2f}, L={ce_data['Low']:.2f}, C={ce_data['Close']:.2f}, V={ce_data['Volume']}")
                    else:
                        if strike != atm_strike:
                            print(f"⚠️  {ce_symbol}: No data (LTP=0, V=0) - Far OTM, contract may be inactive")
            else:
                if not FAST_ENTRY_MODE and strike != atm_strike:
                    print(f"❌ {ce_symbol}: Failed to fetch - Invalid symbol or API error")
            
            # Rate limiting: Add delay between API calls for Angel broker to avoid rate limits
            if isinstance(broker, AngelBroker):
                time.sleep(0.1)  # 100ms delay between symbol fetches (optimized for speed)
            
            # Put Option
            pe_symbol = get_option_symbol(expiry_date, strike, "PE")
            option_symbols.append(pe_symbol)
            pe_data = get_option_ohlcv_realtime(broker, pe_symbol)
            if pe_data:
                pe_data['Strike'] = strike
                pe_data['Option_Type'] = 'PE'
                all_option_data.append(pe_data)
                # Skip verbose prints in fast entry mode
                if not FAST_ENTRY_MODE:
                    if pe_data['LTP'] > 0 or pe_data['Volume'] > 0:
                        delta_str = f", Δ={pe_data['Delta']:.4f}" if pe_data.get('Delta') is not None else ""
                        print(f"✅ {pe_symbol}: LTP={pe_data['LTP']:.2f}, O={pe_data['Open']:.2f}, H={pe_data['High']:.2f}, L={pe_data['Low']:.2f}, C={pe_data['Close']:.2f}, V={pe_data['Volume']}{delta_str}")
                    else:
                        if strike != atm_strike:
                            print(f"⚠️  {pe_symbol}: No data (LTP=0, V=0) - Far OTM, contract may be inactive")
            else:
                if not FAST_ENTRY_MODE and strike != atm_strike:
                    print(f"❌ {pe_symbol}: Failed to fetch - Invalid symbol or API error")
            
            # Rate limiting: Add delay between API calls for Angel broker
            if isinstance(broker, AngelBroker):
                time.sleep(0.1)  # 100ms delay between symbol fetches (optimized for speed)
    
    # Prepare timestamp for later use (but don't save to DB yet in fast mode)
    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    date_str = datetime.now().strftime('%Y-%m-%d')
    
    # Defer database writes and DataFrame creation in fast entry mode
    if not FAST_ENTRY_MODE and all_option_data:
        df_options = pd.DataFrame(all_option_data)
        print("\n" + "="*80)
        print("📊 OPTIONS DATA SUMMARY")
        print("="*80)
        print(df_options.to_string(index=False))
        
        # Save data to database (every 30 seconds by default)
        # Save Nifty spot to database
        save_nifty_spot_to_db(db_conn, nifty_spot, atm_strike, timestamp_str, date_str)
        
        # Save options data to database if it's time for a snapshot
        if should_take_snapshot() and all_option_data:
            save_options_data_to_db(db_conn, all_option_data, timestamp_str, date_str)
            logger.info(f"Data snapshot saved to database (interval: {DATA_SNAPSHOT_INTERVAL}s)")
        
        # Save summary CSV (only when snapshot is taken, not every second)
        if should_take_snapshot():
            log_path = Path(LOG_DIR)
            log_path.mkdir(exist_ok=True)
            csv_filename = log_path / f"options_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df_options.to_csv(csv_filename, index=False)
            print(f"\n💾 Summary saved to: {csv_filename}")
            logger.info(f"Options summary saved to: {csv_filename}")
    
    # =========================================================================
    # 🤖 ALGO TRADING LOGIC
    # =========================================================================
    # Algo trade object already initialized at the start of main()
    
    # Entry condition is bypassed - proceed directly to strike selection
    condition_met = True
    condition_type = "BYPASSED_FOR_TESTING"
    
    # Defer logging in fast entry mode
    if not FAST_ENTRY_MODE:
        print("\n" + "="*80)
        print("STEP 6: ALGO TRADING LOGIC")
        print("="*80)
        logger.info("-" * 100)
        logger.info("ENTRY CONDITION CHECK (BYPASSED - NO VOLUME CALCULATION)")
        logger.info("-" * 100)
        logger.info(f"✅ ENTRY CONDITION BYPASSED - Proceeding directly to strike selection")
        logger.info("-" * 100)
    
    # Check if trade already completed - if yes, skip new trades and just log data
    if algo_trade.trade_completed:
        logger.info("=" * 100)
        logger.info("TRADE ALREADY COMPLETED TODAY - SKIPPING NEW TRADES")
        logger.info("Continuing to log data only...")
        logger.info("=" * 100)
        print(f"\n⚠️  Trade already completed today. Logging data only...")
        condition_met = False  # Don't enter new trades
    
    if condition_met:
        print(f"\n✅ Entry Condition MET: {condition_type}")
        logger.info(f"Entry Condition MET: {condition_type}")
        
        # Step 3: Find strike prices with premiums in range 43-57
        if not FAST_ENTRY_MODE:
            print(f"\n🔍 Searching for strike prices with premiums between ₹{MIN_PREMIUM}-₹{MAX_PREMIUM}...")
            print(f"   Nifty Spot: ₹{nifty_spot:.2f} - Preferring strikes closer to spot price")
        entry_data = find_entry_strikes(all_option_data, nifty_spot)
        
        # Retry loop if no matching strikes found
        retry_count = 0
        while not entry_data:
            if retry_count == 0:
                print(f"\n⚠️  No matching strikes found with:")
                print(f"   - Premium between ₹{MIN_PREMIUM}-₹{MAX_PREMIUM}")
                print(f"   - Total premium <= ₹{MAX_TOTAL_PREMIUM}")
            
            # Check if we should stop retrying
            if STRIKE_SEARCH_MAX_RETRIES > 0 and retry_count >= STRIKE_SEARCH_MAX_RETRIES:
                print(f"\n⏹️  Maximum retries ({STRIKE_SEARCH_MAX_RETRIES}) reached. Stopping search.")
                print(f"💡 Tip: Run the script again when market conditions change")
                logger.info(f"Strike search stopped after {retry_count} retries")
                break
            
            retry_count += 1
            if not FAST_ENTRY_MODE:
                print(f"\n🔄 Retry #{retry_count}: Waiting {STRIKE_SEARCH_RETRY_INTERVAL} seconds before re-checking...")
                print(f"   Press Ctrl+C to stop")
            
            try:
                # Wait before retrying
                time.sleep(STRIKE_SEARCH_RETRY_INTERVAL)
                
                # Re-fetch Nifty spot (it might have changed)
                current_nifty_spot = get_nifty_spot_price(broker)
                if current_nifty_spot:
                    nifty_spot = current_nifty_spot
                    # Recalculate strikes in case spot moved (use fast mode)
                    atm_strike, strikes_above, strikes_below, all_strikes = get_strike_prices(nifty_spot, fast_mode=FAST_ENTRY_MODE)
                    logger.info(f"Retry #{retry_count}: Nifty spot updated to ₹{nifty_spot:.2f}, ATM={atm_strike}")
                
                # Re-fetch option data for all strikes
                if not FAST_ENTRY_MODE:
                    print(f"📊 Re-fetching option data...")
                retry_option_data = []
                for strike in all_strikes:
                    # Call Option
                    ce_symbol = get_option_symbol(expiry_date, strike, "CE", broker)
                    ce_data = get_option_ohlcv_realtime(broker, ce_symbol)
                    if ce_data:
                        ce_data['Strike'] = strike
                        ce_data['Option_Type'] = 'CE'
                        retry_option_data.append(ce_data)
                    
                    # Rate limiting for Angel broker
                    if isinstance(broker, AngelBroker):
                        time.sleep(0.1)  # Optimized for speed
                    
                    # Put Option
                    pe_symbol = get_option_symbol(expiry_date, strike, "PE", broker)
                    pe_data = get_option_ohlcv_realtime(broker, pe_symbol)
                    if pe_data:
                        pe_data['Strike'] = strike
                        pe_data['Option_Type'] = 'PE'
                        retry_option_data.append(pe_data)
                    
                    # Rate limiting for Angel broker
                    if isinstance(broker, AngelBroker):
                        time.sleep(0.1)  # Optimized for speed
                
                # Update all_option_data with fresh data
                all_option_data = retry_option_data
                
                # Try finding entry strikes again with fresh data
                if not FAST_ENTRY_MODE:
                    print(f"🔍 Re-checking for matching strikes...")
                entry_data = find_entry_strikes(all_option_data, nifty_spot)
                
                if entry_data:
                    print(f"\n✅ Found matching strikes on retry #{retry_count}!")
                    break
                else:
                    print(f"   Still no matching strikes found. Will retry again...")
                    
            except KeyboardInterrupt:
                print(f"\n\n⚠️  Strike search stopped by user after {retry_count} retries")
                logger.info(f"Strike search interrupted by user after {retry_count} retries")
                break
            except Exception as e:
                print(f"\n❌ Error during retry #{retry_count}: {e}")
                logger.error(f"Error during strike search retry #{retry_count}: {e}")
                # Continue retrying despite error
                continue
        
        if entry_data:
            call_strike, put_strike, call_price, put_price, call_symbol, put_symbol = entry_data
            total_premium = call_price + put_price
            
            if not FAST_ENTRY_MODE:
                if retry_count > 0:
                    print(f"\n✅ Found matching strikes after {retry_count} retries:")
                else:
                    print(f"\n✅ Found matching strikes:")
                print(f"   Call Strike: {call_strike} @ ₹{call_price:.2f}")
                print(f"   Put Strike: {put_strike} @ ₹{put_price:.2f}")
                print(f"   Total Premium: ₹{total_premium:.2f} (<= ₹{MAX_TOTAL_PREMIUM})")
            
            # Step 4: Enter trade
            algo_trade.enter_trade(call_strike, put_strike, call_price, put_price, call_symbol, put_symbol, broker=broker)
            
            # Save deferred data after trade entry (fast mode optimization)
            if FAST_ENTRY_MODE and all_option_data:
                try:
                    # Save Nifty spot to database
                    save_nifty_spot_to_db(db_conn, nifty_spot, atm_strike, timestamp_str, date_str)
                    # Save options data if it's time for a snapshot
                    if should_take_snapshot():
                        save_options_data_to_db(db_conn, all_option_data, timestamp_str, date_str)
                except Exception as e:
                    logger.error(f"Error saving deferred data: {e}")
            
            # Step 5: Monitor exit condition
            print(f"\n{'='*80}")
            print("STEP 7: MONITORING EXIT CONDITION")
            print(f"{'='*80}")
            # Note: WebSocket handler will be passed if available in next step
            # For now, use API polling with configured interval
            print(f"   Using API polling for exit monitoring (every {API_POLLING_INTERVAL} seconds)...")
            print("   WebSocket will be used if available in next step")
            logger.info("Starting exit monitoring...")
            monitor_exit_condition(broker, algo_trade, handler=None, check_interval=API_POLLING_INTERVAL)
        else:
            # No strikes found after all retries
            print(f"\n❌ No matching strikes found after {retry_count} retries")
            print(f"💡 Tip: Run the script again when market conditions change")
            logger.info(f"Strike search completed without finding matching strikes after {retry_count} retries")
    else:
        print(f"\n❌ Entry Condition NOT MET")
        print(f"\n💡 Waiting for entry condition...")
    
    # Algo trade object already initialized at the start of main()
    
    print("\n" + "="*80)
    print("STEP 5: Real-time Data Monitoring")
    print("="*80)
    
    # Check if WebSocket is enabled and available
    use_websocket = USE_WEBSOCKET and FYERS_WEBSOCKET_AVAILABLE and isinstance(broker, FyersBroker)
    
    if not use_websocket:
        if not USE_WEBSOCKET:
            print("ℹ️  WebSocket is disabled via USE_WEBSOCKET=false")
            print(f"   Using API polling instead (every {API_POLLING_INTERVAL} seconds)")
        elif not FYERS_WEBSOCKET_AVAILABLE:
            print("⚠️  WebSocket functionality is not available.")
            print("   Real-time streaming is disabled. Install fyers-apiv3 package fully.")
            print(f"   Using API polling instead (every {API_POLLING_INTERVAL} seconds)")
        elif not isinstance(broker, FyersBroker):
            print(f"ℹ️  WebSocket not fully implemented for {BROKER.upper()} broker")
            print(f"   Using API polling instead (every {API_POLLING_INTERVAL} seconds)")
        
        # If trade is open, use API polling for monitoring
        if algo_trade.is_position_open:
            print(f"\n🔄 Using API polling for exit monitoring (every {API_POLLING_INTERVAL} seconds)...")
            monitor_exit_condition(broker, algo_trade, handler=None, check_interval=API_POLLING_INTERVAL)
    else:
        # Prepare symbols for WebSocket (include Nifty spot + all option symbols)
        ws_symbols = [NIFTY_SYMBOL] + option_symbols[:20]  # Limit to 20 symbols to avoid overload
        
        print(f"🔌 Connecting to WebSocket for real-time algo data...")
        print(f"📡 Subscribing to {len(ws_symbols)} symbols for live updates")
        print(f"   - Nifty Spot: {NIFTY_SYMBOL}")
        print(f"   - Options: {len(option_symbols[:20])} contracts")
        print()
        print("⚠️  WebSocket will stream real-time data continuously.")
        print("   Press Ctrl+C to stop.")
        print()
        print("="*80)
        print("🔄 REAL-TIME DATA STREAM (For Algo Trading)")
        print("="*80)
        
        try:
            fyers_ws, handler = broker.connect_websocket(ws_symbols, FyersWebSocketHandler(broker.clean_token, ws_symbols))
            if fyers_ws and handler:
                print("\n✅ WebSocket connected! Real-time data streaming active.")
                print("   Use handler.get_realtime_ohlcv(symbol) to access latest data in your algo.")
                
                # If trade is open, update monitoring to use WebSocket
                if algo_trade.is_position_open:
                    print("\n🔄 Switching to WebSocket-based exit monitoring...")
                    monitor_exit_condition(broker, algo_trade, handler=handler, check_interval=API_POLLING_INTERVAL)
            else:
                # WebSocket connection failed, fallback to API polling
                print("\n⚠️  WebSocket connection failed or disabled.")
                print(f"   Falling back to API polling (every {API_POLLING_INTERVAL} seconds)")
                if algo_trade.is_position_open:
                    monitor_exit_condition(broker, algo_trade, handler=None, check_interval=API_POLLING_INTERVAL)
        except KeyboardInterrupt:
            print("\n\n⚠️  WebSocket connection stopped by user.")
            # Fallback to API polling if trade is still open
            if algo_trade.is_position_open:
                print(f"\n🔄 Falling back to API polling (every {API_POLLING_INTERVAL} seconds)...")
                monitor_exit_condition(broker, algo_trade, handler=None, check_interval=API_POLLING_INTERVAL)
        except Exception as e:
            print(f"\n❌ WebSocket connection failed: {e}")
            print(f"   Falling back to API polling (every {API_POLLING_INTERVAL} seconds)")
            # Fallback to API polling
            if algo_trade.is_position_open:
                monitor_exit_condition(broker, algo_trade, handler=None, check_interval=API_POLLING_INTERVAL)
    
    # If trade completed, continue logging data for the rest of the day
    if algo_trade.trade_completed and CONTINUE_LOGGING_AFTER_TRADE:
        print("\n" + "="*80)
        print("📊 TRADE COMPLETED - CONTINUING DATA LOGGING")
        print("="*80)
        print(f"Trade completed. Continuing to log data every {DATA_SNAPSHOT_INTERVAL} seconds...")
        print("Press Ctrl+C to stop.")
        print("="*80)
        logger.info("=" * 100)
        logger.info("TRADE COMPLETED - CONTINUING DATA LOGGING FOR REST OF DAY")
        logger.info("=" * 100)
        
        # Continue logging data in a loop
        try:
            while True:
                time.sleep(DATA_SNAPSHOT_INTERVAL)
                
                # Get fresh data
                current_nifty_spot = get_nifty_spot_price(broker)
                if current_nifty_spot:
                    current_atm = round_to_nearest_50(current_nifty_spot)
                    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    date_str = datetime.now().strftime('%Y-%m-%d')
                    
                    # Save Nifty spot
                    save_nifty_spot_to_db(db_conn, current_nifty_spot, current_atm, timestamp_str, date_str)
                    
                    # Fetch and save options data
                    current_all_option_data = []
                    for strike in all_strikes:
                        ce_symbol = get_option_symbol(expiry_date, strike, "CE")
                        ce_data = get_option_ohlcv_realtime(broker, ce_symbol)
                        if ce_data:
                            ce_data['Strike'] = strike
                            ce_data['Option_Type'] = 'CE'
                            current_all_option_data.append(ce_data)
                        
                        pe_symbol = get_option_symbol(expiry_date, strike, "PE")
                        pe_data = get_option_ohlcv_realtime(broker, pe_symbol)
                        if pe_data:
                            pe_data['Strike'] = strike
                            pe_data['Option_Type'] = 'PE'
                            current_all_option_data.append(pe_data)
                    
                    if current_all_option_data:
                        save_options_data_to_db(db_conn, current_all_option_data, timestamp_str, date_str)
                        logger.info(f"Data Log: Spot=₹{current_nifty_spot:.2f}, ATM={current_atm}")
                    
                    print(f"📊 Data logged at {timestamp_str} - Spot: ₹{current_nifty_spot:.2f}")
                
        except KeyboardInterrupt:
            print("\n\n⚠️  Data logging stopped by user.")
            logger.info("Data logging stopped by user")
        except Exception as e:
            print(f"\n❌ Error during data logging: {e}")
            logger.error(f"Error during data logging: {e}")
    
    print("\n" + "="*80)
    print("✅ Script execution completed!")
    print("="*80)

if __name__ == '__main__':
    try:
        main()
    finally:
        # Log end of day summary
        if logger and db_conn:
            date_str = datetime.now().strftime('%Y-%m-%d')
            log_end_of_day_summary(db_conn, date_str)
        
        # Close database connection
        if db_conn:
            db_conn.close()
            if logger:
                logger.info("Database connection closed")

