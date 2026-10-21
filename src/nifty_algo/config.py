from dataclasses import dataclass
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
import os

# Load .env from project root (folder containing `src/`), not only from cwd — avoids ENABLE_API_SERVER etc. being missed when running from another directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv()  # cwd .env can still override for local experiments

def _bool(val:str | None,default: bool = False) -> bool:
    if val is None or val == "":
        return default
    return str(val).strip().lower() in ("1","true","yes","y")

def _float(val:str | None,default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def _int(val:str | None,default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(val)
    except(ValueError, TypeError):
        return default

def _date_optional(val: str | None) -> date | None:
    """Parse YYYY-MM-DD from env; empty or invalid → None."""
    if val is None or str(val).strip() == "":
        return None
    try:
        return date.fromisoformat(str(val).strip())
    except (ValueError, TypeError):
        return None

def _normalize_fyers_symbol_style(raw: str) -> str:
    """FYERS_OPTION_SYMBOL_STYLE: auto/unpadded | weekly | padded | monthly (+ aliases)."""
    s = (raw or "auto").strip().lower()
    aliases = {
        "compact": "auto",
        "default": "auto",
        "numeric": "auto",
        "unpadded": "auto",  # historical name; means auto weekly/monthly from expiry
        "letter": "monthly",
        "mmm": "monthly",
        "mar": "monthly",  # backward compatibility
        "yymmdd": "padded",
    }
    s = aliases.get(s, s)
    if s not in ("auto", "weekly", "padded", "monthly"):
        return "auto"
    return s

@dataclass(frozen=True)
class Config:
    # --- Strategy ---
    STRATEGY: str
    MAX_TOTAL_PREMIUM: float
    TARGET_PROFIT_POINTS: float

    # --- Strikes ---
    STRIKE_INTERVAL: int
    STRANGLE_ATM_OFFSET_POINTS: int  # backtest: CE=ATM+this, PE=ATM−this (ATM = nearest STRIKE_INTERVAL to spot)
    NUM_STRIKES_ABOVE_BELOW: int
    FAST_ENTRY_MODE: bool
    FAST_ENTRY_MAX_STRIKES: int
    ORDER_QTY: int

    # --- Other strategies (Strangle_0915 doesn't use these) ---
    MIN_PREMIUM: float
    MAX_PREMIUM: float
    VOLUME_RATIO_THRESHOLD: float
    BYPASS_ENTRY_CONDITION: bool

    # --- Broker & run ---
    BROKER: str
    # Historical 1m download for backtest UI (breeze | fyers). Independent of live BROKER.
    HISTORICAL_BROKER: str
    DRY_RUN: bool
    LOG_DIR: str
    USE_DATABASE: bool
    DATABASE_NAME: str

    # --- Entry window (IST) ---
    ENTRY_CUTOFF_ENABLED: bool
    ENTRY_CUTOFF_HOUR: int
    ENTRY_CUTOFF_MINUTE: int

    # --- Fetch / execution ---
    USE_ASYNC_FETCHING: bool
    ASYNC_CONCURRENT_LIMIT: int
    USE_WEBSOCKET: bool
    API_POLLING_INTERVAL: float
    DATA_SNAPSHOT_INTERVAL: int
    NIFTY_PREOPEN_PRICE: float | None
    USE_PREOPEN_PRICE_FOR_STRIKE_SELECTION: bool
    STRIKE_SEARCH_RETRY_INTERVAL: float
    STRIKE_SEARCH_RETRY_INTERVAL_AFTER_9_25: float
    STRIKE_SEARCH_MAX_RETRIES: int

    # --- API Server ---
    ENABLE_API_SERVER: bool
    API_SERVER_PORT: int
    
    # --- UI/Chart Settings ---
    UI_UPDATE_INTERVAL: int  # seconds between UI updates
    CHART_STRANGLE_ONLY: bool  # if True, chart always shows ATM±STRANGLE_ATM_OFFSET_POINTS strangle; algo entry does not overwrite

    # --- Nifty weekly expiry ---
    # Weekday 0=Mon … 6=Sun; NSE weekly default is Tuesday (1). Override with NIFTY_EXPIRY_DATE when not.
    NIFTY_WEEKLY_EXPIRY_WEEKDAY: int
    NIFTY_EXPIRY_DATE: date | None

    # --- Fyers ---
    # Optional Fyers override (default auto): weekly/monthly tickers inferred from expiry date
    FYERS_OPTION_SYMBOL_STYLE: str
    FYERS_OPTION_LETTER_WINDOW_DAYS: int  # legacy; symbol style no longer uses this
    FYERS_CLIENT_ID: str
    FYERS_ACCESS_TOKEN: str

    # --- Angel ---
    ANGEL_API_KEY: str
    ANGEL_CLIENT_CODE: str
    ANGEL_MPIN: str
    ANGEL_TOTP_SECRET: str
    ANGEL_LOAD_CONTRACT_MASTER: bool
    ANGEL_SPOT_RETRY_DELAY: float
    ANGEL_STARTUP_DELAY: float

    # --- ICICI Breeze ---
    BREEZE_API_KEY: str
    BREEZE_API_SECRET: str
    BREEZE_SESSION_TOKEN: str

    @classmethod
    def from_env(cls) -> "Config":
        def env(key:str,default:str | None = None) -> str:
            val = os.getenv(key, default)
            if val is None:
                return ""
            return str(val).strip()

        # Nifty preopen price - None if not set or invalid
        _preopen = env("NIFTY_PREOPEN_PRICE")
        nifty_preopen:float | None = None
        if _preopen:
            try:
                nifty_preopen = float(_preopen)
            except (ValueError, TypeError):
                pass
        
        return cls(
            # Strategy
            STRATEGY=env("STRATEGY", "strangle_0915"),
            MAX_TOTAL_PREMIUM=_float(env("MAX_TOTAL_PREMIUM"), 112.0),
            TARGET_PROFIT_POINTS=_float(env("TARGET_PROFIT_POINTS"), 7.0),
            # Strikes
            STRIKE_INTERVAL=_int(env("STRIKE_INTERVAL"), 50),
            STRANGLE_ATM_OFFSET_POINTS=_int(env("STRANGLE_ATM_OFFSET_POINTS"), 100),
            NUM_STRIKES_ABOVE_BELOW=_int(env("NUM_STRIKES_ABOVE_BELOW"), 5),
            FAST_ENTRY_MODE=_bool(env("FAST_ENTRY_MODE"), True),
            FAST_ENTRY_MAX_STRIKES=_int(env("FAST_ENTRY_MAX_STRIKES"), 4),
            ORDER_QTY=_int(env("ORDER_QTY"), 65),
            # Other strategies
            MIN_PREMIUM=_float(env("MIN_PREMIUM"), 43.0),
            MAX_PREMIUM=_float(env("MAX_PREMIUM"), 57.0),
            VOLUME_RATIO_THRESHOLD=_float(env("VOLUME_RATIO_THRESHOLD"), 2.0),
            BYPASS_ENTRY_CONDITION=_bool(env("BYPASS_ENTRY_CONDITION"), False),
            # Broker & run
            BROKER=env("BROKER", "fyers").lower(),
            HISTORICAL_BROKER=env("HISTORICAL_BROKER", "breeze").lower(),
            DRY_RUN=_bool(env("DRY_RUN"), True),
            LOG_DIR=env("LOG_DIR", "algo_logs"),
            USE_DATABASE=_bool(env("USE_DATABASE"), True),
            DATABASE_NAME=env("DATABASE_NAME", "algo_trading_data.db"),
            # Entry window — new trades only until this IST time (see market.hours.is_within_entry_cutoff)
            ENTRY_CUTOFF_ENABLED=_bool(env("ENTRY_CUTOFF_ENABLED"), True),
            ENTRY_CUTOFF_HOUR=_int(env("ENTRY_CUTOFF_HOUR"), 9),
            ENTRY_CUTOFF_MINUTE=_int(env("ENTRY_CUTOFF_MINUTE"), 20),
            # Fetch / execution
            USE_ASYNC_FETCHING=_bool(env("USE_ASYNC_FETCHING"), True),
            ASYNC_CONCURRENT_LIMIT=_int(env("ASYNC_CONCURRENT_LIMIT"), 10),
            USE_WEBSOCKET=_bool(env("USE_WEBSOCKET"), True),
            API_POLLING_INTERVAL=_float(env("API_POLLING_INTERVAL"), 1.0),
            DATA_SNAPSHOT_INTERVAL=_int(env("DATA_SNAPSHOT_INTERVAL"), 30),
            NIFTY_PREOPEN_PRICE=nifty_preopen,
            USE_PREOPEN_PRICE_FOR_STRIKE_SELECTION=_bool(env("USE_PREOPEN_PRICE_FOR_STRIKE_SELECTION"), False),
            STRIKE_SEARCH_RETRY_INTERVAL=_float(env("STRIKE_SEARCH_RETRY_INTERVAL"), 1.0),
            STRIKE_SEARCH_RETRY_INTERVAL_AFTER_9_25=_float(env("STRIKE_SEARCH_RETRY_INTERVAL_AFTER_9_25"), 5.0),
            STRIKE_SEARCH_MAX_RETRIES=_int(env("STRIKE_SEARCH_MAX_RETRIES"), 0),
            # API Server
            ENABLE_API_SERVER=_bool(env("ENABLE_API_SERVER"), False),
            API_SERVER_PORT=_int(env("API_SERVER_PORT"), 8000),
            # UI/Chart Settings
            UI_UPDATE_INTERVAL=_int(env("UI_UPDATE_INTERVAL"), 60),
            CHART_STRANGLE_ONLY=_bool(env("CHART_STRANGLE_ONLY"), True),
            NIFTY_WEEKLY_EXPIRY_WEEKDAY=_int(env("NIFTY_WEEKLY_EXPIRY_WEEKDAY"), 1),
            NIFTY_EXPIRY_DATE=_date_optional(env("NIFTY_EXPIRY_DATE")),
            # Fyers
            FYERS_OPTION_SYMBOL_STYLE=_normalize_fyers_symbol_style(env("FYERS_OPTION_SYMBOL_STYLE", "auto")),
            FYERS_OPTION_LETTER_WINDOW_DAYS=_int(env("FYERS_OPTION_LETTER_WINDOW_DAYS"), 2),
            FYERS_CLIENT_ID=env("FYERS_CLIENT_ID") or env("CLIENT_ID", ""),
            FYERS_ACCESS_TOKEN=env("FYERS_ACCESS_TOKEN") or env("ACCESS_TOKEN", ""),
            # Angel
            ANGEL_API_KEY=env("ANGEL_API_KEY", ""),
            ANGEL_CLIENT_CODE=env("ANGEL_CLIENT_CODE", ""),
            ANGEL_MPIN=env("ANGEL_MPIN") or env("ANGEL_PASSWORD", ""),
            ANGEL_TOTP_SECRET=env("ANGEL_TOTP_SECRET", ""),
            ANGEL_LOAD_CONTRACT_MASTER=_bool(env("ANGEL_LOAD_CONTRACT_MASTER"), True),
            ANGEL_SPOT_RETRY_DELAY=_float(env("ANGEL_SPOT_RETRY_DELAY"), 5.0),
            ANGEL_STARTUP_DELAY=_float(env("ANGEL_STARTUP_DELAY"), 0),
            # ICICI Breeze
            BREEZE_API_KEY=env("BREEZE_API_KEY", ""),
            BREEZE_API_SECRET=env("BREEZE_API_SECRET", ""),
            BREEZE_SESSION_TOKEN=env("BREEZE_SESSION_TOKEN", ""),
        )
        
# One shared instance: from nifty_algo.config import config
config = Config.from_env()