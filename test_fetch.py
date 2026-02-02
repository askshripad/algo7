import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from nifty_algo.config import config
from nifty_algo.brokers import get_broker
from nifty_algo.core.strikes import get_strike_prices
from nifty_algo.market.expiry import get_nearest_expiry
from nifty_algo.data.fetch import fetch_strikes_smart, get_nifty_spot_price


def main():
    broker = get_broker(config)
    broker.initialize()

    spot = get_nifty_spot_price(broker)
    print("Spot:", spot)
    if spot is None:
        return

    strike_info = get_strike_prices(spot, fast_mode=True)
    atm = strike_info["atm"]
    expiry = get_nearest_expiry()

    data, symbols, _ = fetch_strikes_smart(
        broker,
        expiry,
        atm,
        spot,
        lambda d, s: None,
    )

    print("Fetched options:", len(data))
    print("Symbols sample:", symbols[:4])


if __name__ == "__main__":
    main()
