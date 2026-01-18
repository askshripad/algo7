python generate_token.py
# Select option 2, or it auto-detects if BROKER=angel in .env

# Broker Switch Guide

This guide explains how to switch between Fyers and Angel Smart API brokers.

## Configuration

Add the following to your `.env` file:

### For Fyers:
```env
BROKER=fyers
FYERS_CLIENT_ID=your_client_id
FYERS_ACCESS_TOKEN=your_access_token
```

### For Angel Smart API:
```env
BROKER=angel
ANGEL_API_KEY=your_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_MPIN=your_mpin
ANGEL_TOTP_SECRET=your_totp_secret
```

**Important Notes:**
- **MPIN Required:** Angel One now requires **MPIN** (Mobile PIN) instead of password
- **Static IP Required:** You need a static IP address to create SmartAPI app
  - Get your IP from: https://whatismyipaddress.com/
  - If you don't have static IP, use your current IP and update when it changes
  - See `ANGEL_SMARTAPI_APP_SETUP.md` for detailed instructions
- **Get MPIN:** From Angel One account settings or mobile app

## Installation

Install the required packages:
```bash
pip install -r requirements.txt
```

This will install:
- `fyers-apiv3` (for Fyers)
- `smartapi-python` (for Angel Smart API)
- `pyotp` (for Angel TOTP authentication)

## Switching Brokers

Simply change the `BROKER` value in your `.env` file:
- `BROKER=fyers` - Use Fyers API
- `BROKER=angel` - Use Angel Smart API

The script will automatically:
1. Load the appropriate credentials
2. Initialize the correct broker
3. Use broker-specific symbol formats
4. Handle broker-specific API responses

## Symbol Format Differences

### Fyers:
- Format: `NSE:NIFTY2611326200CE` (YYMDD format)
- Example: `NSE:NIFTY2611326200CE` for Jan 13, 2026, strike 26200

### Angel Smart API:
- Format: `NSE|NIFTY09JAN202526200CE` (DDMMMYY format)
- Example: `NSE|NIFTY09JAN202526200CE` for Jan 9, 2025, strike 26200

The script automatically handles these format differences.

## Features

Both brokers support:
- ✅ Spot price fetching
- ✅ Option quotes (OHLCV)
- ✅ Position checking
- ✅ Order placement
- ✅ WebSocket (Fyers fully supported, Angel in progress)

## Troubleshooting

### Fyers Issues:
- If WebSocket fails, the script falls back to API polling
- Check token expiration and regenerate if needed

### Angel Issues:
- Ensure TOTP secret is correct
- Verify API key and client code
- Check if master file is needed for symbol token lookup

## Notes

- The strategy logic remains the same regardless of broker
- All broker-specific details are abstracted away
- Logs will indicate which broker is being used
