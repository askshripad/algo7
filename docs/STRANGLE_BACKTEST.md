# Strangle backtest & pattern analysis (SQLite)



## What it does



Reads **1-minute** CE/PE rows from `db/strangle_data.db` (`strangle_history`), builds **combined premium** per session, and:



1. **Skips expiry day** — no session where `trade_date == expiry_date`.

2. **Chooses one strangle per trade date** — latest `expiry_date` among non-expiry rows; if several CE/PE pairs exist, picks **highest first-bar open premium** (ATM/liquidity proxy).

3. **Same-day model** — enter at **first bar** combined open, exit at **last bar** combined close.

4. **Volume** — per bar `vol_diff = CE_volume − PE_volume`; session features include **median(|vol_diff|)** and correlations with premium.

5. **ATR / range** — **prior-day ATR** and **trade-day cash range** (High−Low) from `nifty_daily`; **range/ATR** = realized range vs prior ATR.

6. **Short (sell) strangle** — same prices; P&L = **entry open − exit close** (credit minus buyback) = **−long P&L** in points.

7. **Strategy grid** — sums INR P&L for filters:

   - Long when **range/ATR >= 1.0** (expansion vs prior ATR), optionally plus **volume >= median** median(|vol_diff|).

   - Short when **range/ATR <= 0.92** (compression), optionally plus same volume filter.



Thresholds `LONG_RANGE_ATR_MIN` and `SHORT_RANGE_ATR_MAX` are in `strangle_backtest.py` — tune after looking at your month’s `range_div_atr` distribution in `trades.csv`.



## Run



```bash

set PYTHONPATH=src

python -m nifty_algo.analysis.strangle_backtest --month 2025-12

```



## Outputs



| File | Content |

|------|--------|

| `REPORT.md` | Narrative + correlations + strategy grid |

| `trades.csv` | Per session: premium P&L, **short** P&L, volume stats, **prev_day_atr**, **nifty_day_range**, **range_div_atr** |

| `monthly_summary.csv` | Long strangle monthly rollup |

| `monthly_summary_short_strangle.csv` | Short strangle monthly rollup |

| `strategies.csv` / `strategies.txt` | Filtered P&L grid |



## P&L units



- **Points**: long = `exit − entry` on combined premium; short = `entry − exit`.

- **INR**: `points × 50` (one lot per leg; notional research model).



## December 2025 (example; re-run to refresh)



On a sample run with your DB, **unfiltered long** was negative for the month, while:



- **Long + expansion** (`range/ATR >= 1`) and **long + expansion + active volume** improved total INR vs “long all” in that sample.

- **Short all** is the mirror of long all (by construction on the same closes).

- **Short + compression** can help when the day is **quiet vs ATR**; combining with **volume** may help or hurt — check `strategies.csv` for your month.

Fyers issue




**Not financial advice** — add costs, margin, and realistic fills before trading.



## See also



- `src/nifty_algo/analysis/strangle_backtest.py`

- `output/strangle_backtest/2025-12/REPORT.md`

Strategy	Sessions	Sum INR	Win rate
Long — all	19	−5972.5	32%
Long — range/ATR ≥ 1	7	+2772.5	43%
Long — expansion + vol ≥ median	5	+3170	60%
Short — all	19	+5972.5	68% (mirror of long)
Short — range/ATR ≤ 0.92	9	+5287.5	67%
Short — compression + vol	4	−762.5	25% (small sample)

Fyers Issue
What that message means
Fyers is rejecting the order on their side, not because your payload is malformed. The important part is:

Order placement restricted. Algo orders are not allowed from this app KOQIGIKZ1Y-100

So the API app tied to your credentials (KOQIGIKZ1Y-100) is not allowed to place orders that Fyers treats as “algo” (programmatic / API-driven orders under their rules).

Your place_order in fyers.py is a normal market/limit call (INTRADAY, offlineOrder: False, etc.); there is no special “algo” flag in that snippet. Fyers still applies app-level restrictions.

Typical reasons
App not enabled for algo / API trading – The app in the Fyers developer portal may be a type that does not allow automated orders, or algo access was never turned on for that app.
Regulatory / broker onboarding – In India, brokers often require algo registration / Unique Algo ID (UAID) and related approvals before API orders are accepted as “algo.” Until that is done, you can see exactly this kind of rejection.
Wrong or legacy app – Old or duplicate apps sometimes end up restricted after Fyers policy changes; support or a new app with the right permissions is sometimes needed.
What you should do
Fyers developer / My API dashboard – Open the app that matches your client id, and check anything related to algo trading, API order placement, or app permissions. If there is a separate “algo” app or toggle, use what their docs say for live trading.
Fyers support – Quote code -50 and the full message including KOQIGIKZ1Y-100. Ask them to enable order placement for this app or tell you which app type / onboarding (e.g. UAID) is required for your use case.
Do not expect a code-only fix – If the restriction is on the app/account, changing Python alone will not remove it until Fyers enables the right access.
Bottom line: The API is telling you this client app id is not permitted to place (algo-classified) orders. Resolve it with Fyers app settings + support / compliance onboarding, not by tweaking order fields in isolation.


