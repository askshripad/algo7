# Nifty Options Algo Trading Strategy

## Strategy Overview

This algo implements a **volume-based straddle/strangle strategy** that:
1. Monitors OTM (Out-of-The-Money) option volumes
2. Enters trades when volume imbalance is detected
3. Exits when target profit is achieved

## Strategy Logic

### Step 1: Volume Analysis
- **OTM Call Volume**: Sum of volumes for all call strikes **above** ATM
- **OTM Put Volume**: Sum of volumes for all put strikes **below** ATM

### Step 2: Entry Condition
Entry is triggered when:
- **OTM Call Volume >= 2 × OTM Put Volume** OR
- **OTM Put Volume >= 2 × OTM Call Volume**

This indicates a strong directional bias in the market.

### Step 3: Strike Selection
When entry condition is met, the algo searches for:
- **Call strike** with premium (LTP) between **₹43 - ₹57**
- **Put strike** with premium (LTP) between **₹43 - ₹57**
- **Total premium** (Call + Put) **<= ₹100**

If no matching strikes found, the algo waits and checks again.

### Step 4: Trade Entry
Once matching strikes are found:
- **Buy Call** at selected strike
- **Buy Put** at selected strike
- Record entry prices and time

### Step 5: Exit Monitoring
The algo continuously monitors:
- Current Call price (LTP)
- Current Put price (LTP)
- Total current price = Call LTP + Put LTP
- Profit = Current Total - Entry Total

**Exit Condition**: Exit when profit **>= 7 points**

Example:
- Entry Total: ₹99
- Target Exit: ₹106 (99 + 7)
- When Current Total reaches ₹106, exit both positions

## Configuration Parameters

```python
MIN_PREMIUM = 43.0      # Minimum premium for strike selection
MAX_PREMIUM = 57.0      # Maximum premium for strike selection
MAX_TOTAL_PREMIUM = 112.0  # Maximum total premium (call + put)
TARGET_PROFIT_POINTS = 7.0  # Target profit in points to exit
VOLUME_RATIO_THRESHOLD = 2.0  # Volume ratio threshold (2x)
```

## Real-time Data Sources

1. **Initial Data**: Quotes API for snapshot of current prices
2. **Live Monitoring**: WebSocket for real-time price updates
3. **Fallback**: API polling if WebSocket unavailable

## Example Flow

```
1. Nifty Spot: 25995
2. ATM Strike: 26000
3. OTM Call Volume (26050, 26100, ...): 1,000,000
4. OTM Put Volume (25950, 25900, ...): 400,000
5. Condition: Call Volume (1M) >= 2 × Put Volume (400K) ✅
6. Search for strikes with premium 43-57:
   - Found: Call 26050 @ ₹50, Put 25950 @ ₹48
   - Total: ₹98 (<= ₹100) ✅
7. Enter Trade:
   - Buy Call 26050 @ ₹50
   - Buy Put 25950 @ ₹48
   - Entry Total: ₹98
8. Monitor:
   - Current: Call ₹52, Put ₹51 = ₹103
   - Profit: ₹103 - ₹98 = ₹5 (not enough)
   - Continue monitoring...
9. Exit:
   - Current: Call ₹55, Put ₹50 = ₹105
   - Profit: ₹105 - ₹98 = ₹7 ✅
   - Exit both positions
```

## Risk Management

- **Maximum Risk**: Total premium paid (Call + Put)
- **Target Profit**: 7 points
- **Risk-Reward**: Varies based on entry price
- **Stop Loss**: Not implemented (can be added)

## Important Notes

1. **Market Hours**: Strategy works best during market hours (9:15 AM - 3:30 PM IST)
2. **Volume Data**: Requires sufficient volume for accurate analysis
3. **Premium Range**: If no strikes found in 43-57 range, algo waits
4. **Real-time Monitoring**: WebSocket provides fastest exit execution
5. **Manual Override**: Press Ctrl+C to stop monitoring

## Future Enhancements

- Add stop loss functionality
- Add position sizing based on capital
- Add multiple timeframes for volume analysis
- Add backtesting capability
- Add trade logging and performance metrics

