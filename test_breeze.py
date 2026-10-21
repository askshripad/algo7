from datetime import date
from nifty_algo.brokers.breeze import BreezeHistoricalClient, OptionCandle
from nifty_algo.config import config

client = BreezeHistoricalClient(
    api_key=config.BREEZE_API_KEY,
    api_secret=config.BREEZE_API_SECRET,
    session_token=config.BREEZE_SESSION_TOKEN or None,
)
if not client.connect():
    raise SystemExit(f"Breeze connect failed: {client._connect_error}")

ce_strike = 26550
pe_strike = 26050
trade_date = date(2025, 12, 1)
ce_candles, pe_candles = client.get_strangle_historical_1m(
    expiry_date=date(2025, 12, 2),
    ce_strike=ce_strike,
    pe_strike=pe_strike,
    trade_date=trade_date,
)

candles, nifty_src = client.get_nifty_index_historical_1m(
    trade_date=trade_date,
    persist=True,
)

print("CE rows:", len(ce_candles))
print("PE rows:", len(pe_candles))
print("NIFTY rows:", len(candles), "source:", nifty_src)
print("SQLite cache: db/strangle_data.db (tables strangle_history, nifty_index_history)")

# candles = client.get_option_historical_1m(
#     expiry_date=date(2025, 12, 2),
#     strike=26050,
#     option_type="PE",
#     trade_date=date(2025, 12, 1),
# )
#print(len(candles), candles[0] if candles else None)