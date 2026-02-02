from dataclasses import dataclass
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

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

@dataclass(frozen=True)
class Config:
    # --- Strategy ---
    STRATEGY: str
    MAX_TOTAL_PREMIUM: float
    TARGET_PROFIT_POINTS: float

    # --- Strikes ---
    STRIKE_INTERVAL: int
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
    DRY_RUN: bool
    LOG_DIR: str
    USE_DATABASE: bool
    DATABASE_NAME: str

    # --- Fetch / execution ---
    USE_ASYNC_FETCHING: bool
    ASYNC_CONCURRENT_LIMIT: int
    USE_WEBSOCKET: bool
    API_POLLING_INTERVAL: float
    DATA_SNAPSHOT_INTERVAL: int
    NIFTY_PREOPEN_PRICE: float | None
    STRIKE_SEARCH_RETRY_INTERVAL: float
    STRIKE_SEARCH_MAX_RETRIES: int

    # --- Fyers ---
    FYERS_CLIENT_ID: str
    FYERS_ACCESS_TOKEN: str

    # --- Angel ---
    ANGEL_API_KEY: str
    ANGEL_CLIENT_CODE: str
    ANGEL_MPIN: str
    ANGEL_TOTP_SECRET: str
    ANGEL_LOAD_CONTRACT_MASTER: bool
    ANGEL_SPOT_RETRY_DELAY: float

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
            DRY_RUN=_bool(env("DRY_RUN"), True),
            LOG_DIR=env("LOG_DIR", "algo_logs"),
            USE_DATABASE=_bool(env("USE_DATABASE"), True),
            DATABASE_NAME=env("DATABASE_NAME", "algo_trading_data.db"),
            # Fetch / execution
            USE_ASYNC_FETCHING=_bool(env("USE_ASYNC_FETCHING"), True),
            ASYNC_CONCURRENT_LIMIT=_int(env("ASYNC_CONCURRENT_LIMIT"), 10),
            USE_WEBSOCKET=_bool(env("USE_WEBSOCKET"), True),
            API_POLLING_INTERVAL=_float(env("API_POLLING_INTERVAL"), 1.0),
            DATA_SNAPSHOT_INTERVAL=_int(env("DATA_SNAPSHOT_INTERVAL"), 30),
            NIFTY_PREOPEN_PRICE=nifty_preopen,
            STRIKE_SEARCH_RETRY_INTERVAL=_float(env("STRIKE_SEARCH_RETRY_INTERVAL"), 1.0),
            STRIKE_SEARCH_MAX_RETRIES=_int(env("STRIKE_SEARCH_MAX_RETRIES"), 0),
            # Fyers (also support CLIENT_ID / ACCESS_TOKEN as fallback)
            FYERS_CLIENT_ID=env("FYERS_CLIENT_ID") or env("CLIENT_ID", ""),
            FYERS_ACCESS_TOKEN=env("FYERS_ACCESS_TOKEN") or env("ACCESS_TOKEN", ""),
            # Angel
            ANGEL_API_KEY=env("ANGEL_API_KEY", ""),
            ANGEL_CLIENT_CODE=env("ANGEL_CLIENT_CODE", ""),
            ANGEL_MPIN=env("ANGEL_MPIN") or env("ANGEL_PASSWORD", ""),
            ANGEL_TOTP_SECRET=env("ANGEL_TOTP_SECRET", ""),
            ANGEL_LOAD_CONTRACT_MASTER=_bool(env("ANGEL_LOAD_CONTRACT_MASTER"), True),
            ANGEL_SPOT_RETRY_DELAY=_float(env("ANGEL_SPOT_RETRY_DELAY"), 1.0),
        )
        
# One shared instance: from nifty_algo.config import config
config = Config.from_env()