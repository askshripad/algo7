# Volume Range & OTM Range — Calculation Guide

Use this when building the same logic with **broker option-chain data** (not tied to SmartTrade DB/API).

---

## What they measure

At each time snapshot, both metrics answer:

> **Which strike has accumulated the most traded premium (volume × price) since market open?**

| Metric | Call side | Put side |
|--------|-----------|----------|
| **Volume Range** | Best strike among **all** monitored strikes | Best strike among **all** monitored strikes |
| **OTM Range** | Best strike among strikes **above ATM** | Best strike among strikes **below ATM** |

Output format:

```text
{strike}:{premium_crores}Cr
Example: 26200:41Cr
```

Per row you get four values:

```text
call_volume_range       → e.g. 26250:41Cr
put_volume_range        → e.g. 26100:38Cr
call_otm_volume_range   → e.g. 26300:25Cr
put_otm_volume_range    → e.g. 26050:30Cr
```

---

## Inputs (from broker)

For each `(date, time, expiry, strike)` snapshot you need:

| Field | Used for |
|-------|----------|
| `call_volume` | Cumulative call volume (lots) for the day |
| `call_last_price` | Call LTP at that time |
| `put_volume` | Cumulative put volume (lots) for the day |
| `put_last_price` | Put LTP at that time |
| `spot_price` | Spot / underlying price (for ATM) |

Also configure:

- `lot_size` — e.g. 75 (Nifty), 50 (Bank Nifty), or broker contract size
- `volume_start_time` — **09:20:00** (ignore snapshots before this)
- `strike_step` — e.g. 50 for index options

---

## Step 1 — ATM strike

At each timestamp:

```python
atm_strike = strike closest to spot_price
# e.g. min(strikes, key=lambda s: abs(s - spot_price))
```

---

## Step 2 — Strike universe (optional band)

SmartTrade homepage limits strikes to ATM ± 5 (step 50):

- **Calls:** ATM−50 … ATM+250 (ATM−1 to ATM+5)
- **Puts:** ATM+50 … ATM−250 (ATM+1 to ATM−5)

If your broker feed has fewer strikes, use whatever is available. The algorithm is the same.

---

## Step 3 — Incremental volume (critical)

Broker `volume` is **cumulative for the day**. Process snapshots in **chronological order (ASC)**.

For each strike, at each time `t`:

```python
if strike not seen_before:
    incremental = current_volume
else:
    incremental = current_volume - previous_volume

if incremental < 0:
    skip  # bad data or reset

previous_volume[strike] = current_volume
```

Do this separately for call and put side.

---

## Step 4 — Accumulate premium (rupees)

Only when `incremental > 0` and `last_price > 0`:

```python
premium_rs = incremental_lots * lot_size * last_price

call_accumulated_premium[strike] += premium_rs
put_accumulated_premium[strike]  += premium_rs
```

State (`call_accumulated_premium`, `put_accumulated_premium`, `previous_volume`) **carries forward** across the whole session from 09:20 onward. At 11:00, ranges reflect everything since 09:20, not just the last bar.

Convert to crores:

```python
premium_crs = premium_rs / 10_000_000
```

---

## Step 5 — Volume Range (all strikes)

At each time row, pick the strike with **maximum accumulated premium**:

```python
call_volume_range_strike = strike with max(call_accumulated_premium)
call_volume_range_premium_crs = max(call_accumulated_premium) / 1e7

put_volume_range_strike = strike with max(put_accumulated_premium)
put_volume_range_premium_crs = max(put_accumulated_premium) / 1e7
```

Format:

```python
call_volume_range = f"{int(call_strike)}:{int(call_premium_crs)}Cr"
put_volume_range  = f"{int(put_strike)}:{int(put_premium_crs)}Cr"
```

Use `"-"` if no strike qualifies.

---

## Step 6 — OTM Range (ATM-filtered)

Same accumulated premiums, but **filter by ATM**:

```python
# Call OTM: strike > atm_strike
call_otm_candidates = {s: p for s, p in call_accumulated_premium.items() if s > atm_strike}
call_otm_strike = max(call_otm_candidates, key=call_otm_candidates.get)

# Put OTM: strike < atm_strike
put_otm_candidates = {s: p for s, p in put_accumulated_premium.items() if s < atm_strike}
put_otm_strike = max(put_otm_candidates, key=put_otm_candidates.get)
```

Format same as Volume Range:

```python
call_otm_volume_range = f"{int(call_otm_strike)}:{int(call_otm_premium_crs)}Cr"
put_otm_volume_range  = f"{int(put_otm_strike)}:{int(put_otm_premium_crs)}Cr"
```

---

## Reference implementation (minimal)

```python
from datetime import time

LOT_SIZE = 75
VOLUME_START = time(9, 20, 0)

def fmt(strike, premium_crs):
    return f"{int(strike)}:{int(premium_crs)}Cr" if strike else "-"

def max_strike_premium(acc: dict):
    if not acc:
        return None, 0.0
    strike = max(acc, key=acc.get)
    return strike, acc[strike] / 1e7

def compute_ranges(snapshots, atm_by_time):
    """
    snapshots: list sorted by time ASC
      each: {time, strike, call_volume, call_last_price, put_volume, put_last_price}
    atm_by_time: {time -> atm_strike}
    """
    call_prev, put_prev = {}, {}
    call_acc, put_acc = {}, {}
    rows = []

    for snap in snapshots:
        t = snap["time"]
        if t < VOLUME_START:
            continue

        strike = snap["strike"]

        # Call incremental + accumulate
        cv, cp = snap["call_volume"], snap["call_last_price"]
        inc = cv - call_prev.get(strike, 0)
        if inc < 0:
            inc = 0
        call_prev[strike] = cv
        if inc > 0 and cp > 0:
            call_acc[strike] = call_acc.get(strike, 0) + inc * LOT_SIZE * cp

        # Put incremental + accumulate
        pv, pp = snap["put_volume"], snap["put_last_price"]
        inc = pv - put_prev.get(strike, 0)
        if inc < 0:
            inc = 0
        put_prev[strike] = pv
        if inc > 0 and pp > 0:
            put_acc[strike] = put_acc.get(strike, 0) + inc * LOT_SIZE * pp

        atm = atm_by_time.get(t)

        cs, cc = max_strike_premium(call_acc)
        ps, pc = max_strike_premium(put_acc)

        call_otm = {s: v for s, v in call_acc.items() if atm and s > atm}
        put_otm  = {s: v for s, v in put_acc.items()  if atm and s < atm}
        cos, coc = max_strike_premium(call_otm)
        pos, poc = max_strike_premium(put_otm)

        rows.append({
            "time": t,
            "call_volume_range": fmt(cs, cc),
            "put_volume_range": fmt(ps, pc),
            "call_otm_volume_range": fmt(cos, coc),
            "put_otm_volume_range": fmt(pos, poc),
            "call_volume_range_premium_crs": cc,
            "put_volume_range_premium_crs": pc,
        })

    return rows
```

---

## Signal (Volume Range only)

Compare **Volume Range** premiums (not OTM Range) at each time row:

```python
put_premium_crs = put_volume_range_premium_crs   # from max put accumulated premium
call_premium_crs = call_volume_range_premium_crs # from max call accumulated premium

if put_premium_crs > call_premium_crs:
    # Signal: more money flowing into puts
    # Often read as bullish hedging / put writing
    signal = "BULLISH_PUT_FLOW"
```

**Meaning:** The put side strike with the highest cumulative traded premium (since 09:20) has more total money than the call side’s top strike — indicates heavier put-side volume activity.

---

## Checklist for broker integration

1. Sort all option-chain ticks **oldest → newest**
2. Start accumulating only from **09:20**
3. Use **delta volume** (current − previous), not raw volume per bar
4. Premium = `incremental_lots × lot_size × last_price`
5. Keep running totals per strike for the full session
6. Volume Range = max premium across all strikes (call / put separately)
7. OTM Range = same, but call `strike > ATM`, put `strike < ATM`
8. Signal: `put_volume_range_premium_crs > call_volume_range_premium_crs`

---

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Using bar volume instead of cumulative delta | Subtract previous snapshot volume |
| Processing times out of order | Sort ASC by timestamp |
| Using strike × volume for premium | Use `last_price × lots × lot_size` |
| Resetting accumulators each bar | Carry state from 09:20 to close |
| Comparing OTM premiums for the signal | Signal uses **Volume Range** premiums only |
