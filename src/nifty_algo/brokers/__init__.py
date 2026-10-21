from .base import BrokerInterface
from .angel import AngelBroker

try:
    from .breeze import BreezeHistoricalClient, OptionCandle
except Exception:
    BreezeHistoricalClient = None
    OptionCandle = None

try:
    from .fyers import FyersBroker
except Exception:
    FyersBroker = None

try:
    from .fyers_historical import FyersHistoricalClient
except Exception:
    FyersHistoricalClient = None

def get_broker(config) -> BrokerInterface:
    if config.BROKER == 'fyers':
        if FyersBroker is None:
            raise ImportError("Fyers broker is not available in this build.")
        return FyersBroker(
            client_id=config.FYERS_CLIENT_ID,
            access_token=config.FYERS_ACCESS_TOKEN,
        )
    elif config.BROKER == 'angel':
        return AngelBroker(
            api_key=config.ANGEL_API_KEY,
            client_code=config.ANGEL_CLIENT_CODE,
            mpin=config.ANGEL_MPIN,
            totp_secret=config.ANGEL_TOTP_SECRET,
            log_dir=config.LOG_DIR,
            load_contract_master=config.ANGEL_LOAD_CONTRACT_MASTER,
        )

    raise ValueError(f"Unknown broker: {config.BROKER}")


def get_historical_client(config=None):
    """
    Client for 1m historical download (backtest). Uses ``HISTORICAL_BROKER``
    (breeze | fyers), independent of live ``BROKER``.
    """
    if config is None:
        from nifty_algo.config import config as _cfg

        config = _cfg

    name = (getattr(config, "HISTORICAL_BROKER", None) or "breeze").strip().lower()
    if name == "fyers":
        if FyersHistoricalClient is None:
            raise ImportError("Fyers historical client is not available in this build.")
        return FyersHistoricalClient(
            client_id=config.FYERS_CLIENT_ID,
            access_token=config.FYERS_ACCESS_TOKEN,
            log_dir=config.LOG_DIR,
        )
    if name == "breeze":
        if BreezeHistoricalClient is None:
            raise ImportError("Breeze historical client is not available in this build.")
        return BreezeHistoricalClient(
            api_key=config.BREEZE_API_KEY,
            api_secret=config.BREEZE_API_SECRET,
            session_token=config.BREEZE_SESSION_TOKEN or None,
        )
    raise ValueError(f"Unknown HISTORICAL_BROKER: {name} (use breeze or fyers)")


__all__ = [
    "AngelBroker",
    "BrokerInterface",
    "BreezeHistoricalClient",
    "FyersBroker",
    "FyersHistoricalClient",
    "get_broker",
    "get_historical_client",
    "OptionCandle",
]
