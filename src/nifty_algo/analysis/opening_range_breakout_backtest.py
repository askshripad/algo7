"""
Opening-range breakout backtest (Nifty + combined strangle premium, 1-minute data).

Rules (IST, naive timestamps in DB = exchange local time):

1. **No entries before 10:01** — the opening window completes first.

2. **Opening window** (09:15–10:00 inclusive): record **closes only** (no wicks):
   - Nifty: **max** and **min** of **1m closes** in the window
   - Premium: **max** and **min** of **combined closes** (CE close + PE close) in the window

3. **From 10:01 onward**, scan for the **first** 1m bar where **both** confirm on **that bar’s
   close** (same bar):
   - **Long**: Nifty close > opening-range **max close** **and** premium combined close > premium
     **max close** in the window
   - **Short**: Nifty close < opening-range **min close** **and** premium combined close <
     premium **min close** in the window
   Long is checked before short on each bar.

4. **Freshness (longs only)** — gap = bars **strictly after 10:00**, before the signal bar:
   - **Long**: skip if any bar in the gap has Nifty **close** or premium **close** above the
     opening-range **max closes** (``skipped_prior_high``). Prior **closes** below the range min
     are ignored.
   - **Short**: **no** prior-bar filter — spikes **above** the opening range (09:15–low break) are
     ignored, and earlier **dips** below the range are also **not** used to veto the short (only
     the first close-confirmed breakdown after 10:01 matters).

5. **SL / target (combined premium)** use **running max/min close** from **09:15** through the
   **entry** bar (inclusive):
   - **Long**: SL = min close so far − 1; arm = entry close − min close; TP = entry + 2 × arm.
   - **Short**: SL = max close so far + 1; arm = max close − entry close; TP = entry − 2 × arm
     (floored at 0).

6. **Opening-range width** (09:15–10:00 premium high − low) is still computed for reporting and
   for ``no_signal`` rows (``distance_premium``).

7. **Exit**: walk minute bars after entry; SL/TP vs **intrabar** combined premium high/low
   (conservative). Last bar of session forces exit at **close** if still open.

Requires ``db/strangle_data.db`` with ``strangle_history`` (CE/PE OHLC) and ``nifty_index_history``
(NIFTY 1m) for the same ``trade_date``.

Research only — not financial advice.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

import pandas as pd

from nifty_algo.analysis.strangle_backtest import (
    SessionKey,
    _find_project_root,
    discover_pairs,
    load_month_frame,
    pick_session_pair,
)
from nifty_algo.data.strangle_history_db import get_strangle_db_path

LOT_SIZE = 65
BROKERAGE_INR = 200.0  # flat per completed trade (entry + exit)

# IST session times (naive)
MARKET_OPEN = time(9, 15)
OPEN_RANGE_END = time(10, 0)  # inclusive: range high/low use 09:15–10:00 bars
SIGNAL_START = time(10, 1)  # first bar at or after this time may enter (after 10:00 candle)
SESSION_END = time(15, 30)


def _parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def merge_premium_ohlc(conn: sqlite3.Connection, k: SessionKey) -> pd.DataFrame:
    q = """
    SELECT timestamp, open, high, low, close, volume FROM strangle_history
    WHERE trade_date = ? AND expiry_date = ? AND option_side = ? AND strike = ?
    ORDER BY timestamp
    """
    ce = pd.read_sql_query(
        q, conn, params=(k.trade_date, k.expiry_date, "CE", k.ce_strike)
    )
    pe = pd.read_sql_query(
        q, conn, params=(k.trade_date, k.expiry_date, "PE", k.pe_strike)
    )
    if ce.empty or pe.empty:
        return pd.DataFrame()
    m = ce.merge(pe, on="timestamp", suffixes=("_ce", "_pe"))
    for col in ("open", "high", "low", "close"):
        m[f"{col}_ce"] = pd.to_numeric(m[f"{col}_ce"], errors="coerce")
        m[f"{col}_pe"] = pd.to_numeric(m[f"{col}_pe"], errors="coerce")
    m["total_open"] = m["open_ce"] + m["open_pe"]
    m["total_high"] = m["high_ce"] + m["high_pe"]
    m["total_low"] = m["low_ce"] + m["low_pe"]
    m["total_close"] = m["close_ce"] + m["close_pe"]
    m["ts"] = _parse_ts(m["timestamp"])
    m = m.dropna(subset=["ts"])
    return m.sort_values("ts").reset_index(drop=True)


def load_nifty_1m(conn: sqlite3.Connection, trade_date: str) -> pd.DataFrame:
    q = """
    SELECT timestamp, open, high, low, close, volume
    FROM nifty_index_history
    WHERE trade_date = ?
    ORDER BY timestamp ASC
    """
    df = pd.read_sql_query(q, conn, params=(trade_date,))
    if df.empty:
        return df
    df["ts"] = _parse_ts(df["timestamp"])
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)


def align_premium_nifty(prem: pd.DataFrame, nx: pd.DataFrame) -> pd.DataFrame:
    """Inner join on minute timestamp."""
    if prem.empty or nx.empty:
        return pd.DataFrame()
    a = prem[
        [
            "ts",
            "timestamp",
            "total_open",
            "total_high",
            "total_low",
            "total_close",
        ]
    ].copy()
    b = nx[["ts", "timestamp", "open", "high", "low", "close"]].rename(
        columns={
            "open": "n_open",
            "high": "n_high",
            "low": "n_low",
            "close": "n_close",
        }
    )
    j = a.merge(b, on="ts", how="inner", suffixes=("", "_n"))
    return j.sort_values("ts").reset_index(drop=True)


def opening_range_mask(ts: pd.Series) -> pd.Series:
    """Bars from first >= MARKET_OPEN through <= OPEN_RANGE_END (same calendar day)."""
    t = ts.dt.time
    return (t >= MARKET_OPEN) & (t <= OPEN_RANGE_END)


def after_signal_mask(ts: pd.Series) -> pd.Series:
    return ts.dt.time >= SIGNAL_START


def gap_bars_after_open_range_before_signal(
    aligned: pd.DataFrame, entry_pos: int
) -> pd.DataFrame:
    """Bars with time strictly after OPEN_RANGE_END (10:00), indices ``0 .. entry_pos-1`` (excludes signal bar)."""
    if entry_pos <= 0:
        return aligned.iloc[0:0]
    idx = [
        i
        for i in range(entry_pos)
        if aligned.iloc[i]["ts"].time() > OPEN_RANGE_END
    ]
    return aligned.iloc[idx] if idx else aligned.iloc[0:0]


def long_blocked_by_prior_new_high(
    aligned: pd.DataFrame, entry_pos: int, nh: float, ph: float
) -> bool:
    """nh/ph = opening-range **max closes**; gap uses **closes** only (no wicks)."""
    gap = gap_bars_after_open_range_before_signal(aligned, entry_pos)
    if gap.empty:
        return False
    return bool(gap["n_close"].max() > nh or gap["total_close"].max() > ph)


@dataclass
class SimResult:
    side: str  # "long" | "short"
    entry_idx: int
    entry_ts: Any
    entry_premium: float
    exit_ts: Any
    exit_premium: float
    exit_reason: str
    pnl_points: float
    morning_nh: float
    morning_nl: float
    morning_ph: float
    morning_pl: float
    morning_distance: float
    run_high_prem: float
    run_low_prem: float
    tp_arm: float
    sl: float
    tp: float


def _running_premium_extremes_to_entry(df: pd.DataFrame, entry_pos: int) -> tuple[float, float]:
    """Max / min **combined premium close** from session start through entry bar (inclusive)."""
    sub = df.iloc[0 : entry_pos + 1]
    return float(sub["total_close"].max()), float(sub["total_close"].min())


def simulate_trade(
    df: pd.DataFrame,
    entry_pos: int,
    side: str,
    morning_nh: float,
    morning_nl: float,
    morning_ph: float,
    morning_pl: float,
    morning_distance: float,
) -> SimResult | None:
    row = df.iloc[entry_pos]
    entry_p = float(row["total_close"])
    run_hi, run_lo = _running_premium_extremes_to_entry(df, entry_pos)
    if side == "long":
        tp_arm = entry_p - run_lo
        if tp_arm <= 0:
            return None
        sl = run_lo - 1.0
        tp = entry_p + 2.0 * tp_arm
    else:
        tp_arm = run_hi - entry_p
        if tp_arm <= 0:
            return None
        sl = run_hi + 1.0
        tp = max(0.0, entry_p - 2.0 * tp_arm)

    for j in range(entry_pos, len(df)):
        bar = df.iloc[j]
        hi = float(bar["total_high"])
        lo = float(bar["total_low"])
        cl = float(bar["total_close"])
        ts = bar["ts"]
        if side == "long":
            if lo <= sl:
                return SimResult(
                    side=side,
                    entry_idx=entry_pos,
                    entry_ts=df.iloc[entry_pos]["ts"],
                    entry_premium=entry_p,
                    exit_ts=ts,
                    exit_premium=sl,
                    exit_reason="SL",
                    pnl_points=sl - entry_p,
                    morning_nh=morning_nh,
                    morning_nl=morning_nl,
                    morning_ph=morning_ph,
                    morning_pl=morning_pl,
                    morning_distance=morning_distance,
                    run_high_prem=run_hi,
                    run_low_prem=run_lo,
                    tp_arm=tp_arm,
                    sl=sl,
                    tp=tp,
                )
            if hi >= tp:
                return SimResult(
                    side=side,
                    entry_idx=entry_pos,
                    entry_ts=df.iloc[entry_pos]["ts"],
                    entry_premium=entry_p,
                    exit_ts=ts,
                    exit_premium=tp,
                    exit_reason="TP",
                    pnl_points=tp - entry_p,
                    morning_nh=morning_nh,
                    morning_nl=morning_nl,
                    morning_ph=morning_ph,
                    morning_pl=morning_pl,
                    morning_distance=morning_distance,
                    run_high_prem=run_hi,
                    run_low_prem=run_lo,
                    tp_arm=tp_arm,
                    sl=sl,
                    tp=tp,
                )
        else:
            if hi >= sl:
                return SimResult(
                    side=side,
                    entry_idx=entry_pos,
                    entry_ts=df.iloc[entry_pos]["ts"],
                    entry_premium=entry_p,
                    exit_ts=ts,
                    exit_premium=sl,
                    exit_reason="SL",
                    pnl_points=entry_p - sl,
                    morning_nh=morning_nh,
                    morning_nl=morning_nl,
                    morning_ph=morning_ph,
                    morning_pl=morning_pl,
                    morning_distance=morning_distance,
                    run_high_prem=run_hi,
                    run_low_prem=run_lo,
                    tp_arm=tp_arm,
                    sl=sl,
                    tp=tp,
                )
            if lo <= tp:
                return SimResult(
                    side=side,
                    entry_idx=entry_pos,
                    entry_ts=df.iloc[entry_pos]["ts"],
                    entry_premium=entry_p,
                    exit_ts=ts,
                    exit_premium=tp,
                    exit_reason="TP",
                    pnl_points=entry_p - tp,
                    morning_nh=morning_nh,
                    morning_nl=morning_nl,
                    morning_ph=morning_ph,
                    morning_pl=morning_pl,
                    morning_distance=morning_distance,
                    run_high_prem=run_hi,
                    run_low_prem=run_lo,
                    tp_arm=tp_arm,
                    sl=sl,
                    tp=tp,
                )

    last = df.iloc[-1]
    cl = float(last["total_close"])
    return SimResult(
        side=side,
        entry_idx=entry_pos,
        entry_ts=df.iloc[entry_pos]["ts"],
        entry_premium=entry_p,
        exit_ts=last["ts"],
        exit_premium=cl,
        exit_reason="EOD",
        pnl_points=(cl - entry_p) if side == "long" else (entry_p - cl),
        morning_nh=morning_nh,
        morning_nl=morning_nl,
        morning_ph=morning_ph,
        morning_pl=morning_pl,
        morning_distance=morning_distance,
        run_high_prem=run_hi,
        run_low_prem=run_lo,
        tp_arm=tp_arm,
        sl=sl,
        tp=tp,
    )


def run_orb_backtest(
    conn: sqlite3.Connection,
    month_prefix: str,
    *,
    skip_expiry_day: bool,
    skip_freshness_filter: bool = False,
) -> pd.DataFrame:
    dfm = load_month_frame(conn, month_prefix)
    if dfm.empty:
        return pd.DataFrame()

    pairs = discover_pairs(dfm)
    trade_days = sorted({p.trade_date for p in pairs})

    rows: list[dict[str, Any]] = []

    for td in trade_days:
        chosen = pick_session_pair(pairs, td, skip_expiry_day=skip_expiry_day)
        if chosen is None:
            continue
        same_exp = [p for p in pairs if p.trade_date == td and p.expiry_date == chosen.expiry_date]
        if len(same_exp) > 1:
            best = None
            best_open = -1.0
            for k in same_exp:
                m = merge_premium_ohlc(conn, k)
                if m.empty:
                    continue
                o = float(m.iloc[0]["total_open"])
                if o > best_open:
                    best_open = o
                    best = k
            if best is not None:
                chosen = best

        prem = merge_premium_ohlc(conn, chosen)
        nifty = load_nifty_1m(conn, chosen.trade_date)
        if prem.empty or nifty.empty:
            continue

        aligned = align_premium_nifty(prem, nifty)
        if aligned.empty or len(aligned) < 30:
            continue

        om = opening_range_mask(aligned["ts"])
        if not om.any():
            continue

        # Opening range: **closes only** (max/min of 1m closes in 09:15–10:00)
        nh = float(aligned.loc[om, "n_close"].max())
        nl = float(aligned.loc[om, "n_close"].min())
        ph = float(aligned.loc[om, "total_close"].max())
        pl = float(aligned.loc[om, "total_close"].min())
        distance = ph - pl
        if distance <= 0:
            continue

        if not after_signal_mask(aligned["ts"]).any():
            continue

        entry_pos = None
        side = None
        for i in range(len(aligned)):
            if aligned.iloc[i]["ts"].time() < SIGNAL_START:
                continue
            nc = float(aligned.iloc[i]["n_close"])
            pc = float(aligned.iloc[i]["total_close"])
            if nc > nh and pc > ph:
                entry_pos = i
                side = "long"
                break
            if nc < nl and pc < pl:
                entry_pos = i
                side = "short"
                break

        if entry_pos is None or side is None:
            rows.append(
                {
                    "trade_date": td,
                    "expiry_date": chosen.expiry_date,
                    "ce_strike": chosen.ce_strike,
                    "pe_strike": chosen.pe_strike,
                    "status": "no_signal",
                    "morning_nifty_high": nh,
                    "morning_nifty_low": nl,
                    "morning_premium_high": ph,
                    "morning_premium_low": pl,
                    "distance_premium": distance,
                }
            )
            continue

        if (
            not skip_freshness_filter
            and side == "long"
            and long_blocked_by_prior_new_high(aligned, entry_pos, nh, ph)
        ):
            rows.append(
                {
                    "trade_date": td,
                    "expiry_date": chosen.expiry_date,
                    "ce_strike": chosen.ce_strike,
                    "pe_strike": chosen.pe_strike,
                    "status": "skipped_prior_high",
                    "morning_nifty_high": nh,
                    "morning_nifty_low": nl,
                    "morning_premium_high": ph,
                    "morning_premium_low": pl,
                    "distance_premium": distance,
                }
            )
            continue
        sim = simulate_trade(aligned, entry_pos, side, nh, nl, ph, pl, distance)
        if sim is None:
            continue

        rows.append(
            {
                "trade_date": td,
                "expiry_date": chosen.expiry_date,
                "ce_strike": chosen.ce_strike,
                "pe_strike": chosen.pe_strike,
                "status": "traded",
                "side": sim.side,
                "morning_nifty_high": nh,
                "morning_nifty_low": nl,
                "morning_premium_high": ph,
                "morning_premium_low": pl,
                "distance_premium": distance,
                "run_premium_high_to_entry": sim.run_high_prem,
                "run_premium_low_to_entry": sim.run_low_prem,
                "tp_arm_premium": sim.tp_arm,
                "entry_ts": str(sim.entry_ts),
                "exit_ts": str(sim.exit_ts),
                "entry_premium": sim.entry_premium,
                "exit_premium": sim.exit_premium,
                "exit_reason": sim.exit_reason,
                "sl": sim.sl,
                "tp": sim.tp,
                "pnl_points": sim.pnl_points,
                "pnl_inr": sim.pnl_points * LOT_SIZE - BROKERAGE_INR,
            }
        )

    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ORB strangle backtest: range 09:15–10:00, signals from 10:01 (see module doc)"
    )
    ap.add_argument("--month", default="2025-12", help="YYYY-MM trade_date prefix")
    ap.add_argument("--db", type=Path, default=None, help="Path to strangle_data.db")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output CSV path (default: output/orb_backtest/<month>/trades.csv)",
    )
    ap.add_argument(
        "--no-freshness-filter",
        action="store_true",
        help="Disable prior fake-breakout filter for longs only (see module docstring)",
    )
    args = ap.parse_args()
    db_path = args.db or get_strangle_db_path()
    root = _find_project_root()
    out_csv = args.out or (root / "output" / "orb_backtest" / args.month / "trades.csv")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        trades = run_orb_backtest(
            conn,
            args.month,
            skip_expiry_day=True,
            skip_freshness_filter=args.no_freshness_filter,
        )
    finally:
        conn.close()

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out_csv, index=False)
    print(f"Wrote {out_csv} ({len(trades)} rows)")
    if not trades.empty and "pnl_inr" in trades.columns:
        sub = trades[trades["status"] == "traded"]
        if not sub.empty:
            print(
                f"Traded sessions: {len(sub)} | Sum INR: {sub['pnl_inr'].sum():.1f} | "
                f"Win rate: {(sub['pnl_inr'] > 0).mean():.1%}"
            )


if __name__ == "__main__":
    main()
