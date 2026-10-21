# Strangle backtest — trade_date LIKE `2026-01%`

## Rules

- **Long strangle**: entry at **first bar combined premium** (CE open + PE open), exit at **last bar combined close**.
- **Short strangle (sell)**: same prices — P&L = **entry open − exit close** (credit minus buyback) = opposite of long in points.
- **No trades on expiry day**: sessions where `trade_date == expiry_date` are **excluded**.
- If multiple CE/PE pairs exist for a day, pick the **latest expiry** for that date, then the pair with **highest first-bar open premium** (ATM/liquidity proxy).
- P&L: `× 50` INR per combined strangle (1 lot CE + 1 lot PE; notional model).

## Monthly P&L — long strangle

```
  month  sessions  pnl_inr_sum  pnl_points_sum  win_rate
2026-01        16       -110.0            -2.2    0.3125
```

## Monthly P&L — short (sell) strangle (all sessions)

```
  month  sessions  pnl_short_inr_sum  win_rate_short
2026-01        16              110.0          0.6875
```

## Volume + ATR filters (research)

- **Volume**: per minute `vol_diff = CE_volume − PE_volume`; session feature = **median(|vol_diff|)**. “Vol active” = above **median** of that column for the month.
- **ATR**: **prev-day ATR** from `nifty_daily` (row before trade date). **Day range** = High−Low for **trade date** from `nifty_daily`. **range/ATR** compares realized cash range to prior ATR.
- **Long filter**: consider entries when **range/ATR >= 1.0** (expansion vs prior ATR).
- **Short (sell) filter**: consider when **range/ATR <= 0.92** (compression -- short vol friendly in idealized backtest).

### Strategy grid (sum of INR P&L over filtered sessions)

```
                                              name  n  pnl_inr  win_rate
                              Long -- all sessions 16   -110.0  0.312500
 Long -- range/ATR >= 1.0 (expansion vs prior ATR) 10   5820.0  0.400000
Long -- expansion + vol >= median |CE-PE| vol diff  7    302.5  0.285714
             Short (sell strangle) -- all sessions 16    110.0  0.687500
          Short -- range/ATR <= 0.92 (compression)  5   5405.0  0.800000
   Short -- compression + vol >= median |vol diff|  1   1012.5  1.000000
```

## Pattern metrics (same-day hold)

- Sessions: **16**
- Mean P&L long (points): **-0.14**
- Median P&L long (points): **-6.65**
- Win rate long (days > 0): **31.2%**
- Corr(entry open premium, long P&L points): **-0.41812427740522795**
- Corr(median |vol_diff|, long P&L points): **0.09150472926170938**
- Corr(range/ATR, long P&L points): **0.5707640271226001**

### P&L by tercile of entry open premium (same month)

```
                   mean  count
_open_bucket                  
low            3.441667      6
mid           15.175000      4
high         -13.925000      6
```

### Interpretation (heuristic)

- **Long**: Favour **expansion** days (high **range/ATR**) and enough **two-sided flow** (median |vol diff|).
- **Short (sell strangle)**: In idealized same-day terms, **compression** (low **range/ATR**) often aligns with **positive short-strangle** P&L if premium decays — validate on `strategies` table and your costs.
- **When to buy (enter)**: Prefer entries when combined premium is **cheap vs recent range** (e.g. lower tercile of day-open premium) *if* your stats show positive mean P&L in that bucket — see `pnl_by_open_tercile` in the run log.
- **When to sell (exit)**: Same-day model exits at **close**; intraday, consider taking profit when combined premium **reverts toward mean** after a spike, or **cut** if premium bleeds with low realized Nifty range.
- **Expiry**: do **not** initiate or hold a discretionary strangle into **expiry day** (gamma/pin risk); this backtest already skips `trade_date == expiry_date`.
