from .base import BrokerInterface
from .angel import AngelBroker

try:
    from .fyers import FyersBroker
except Exception:
    FyersBroker = None

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
