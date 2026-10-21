"""
Discover strangle sessions in ``strangle_data.db``, analyse simple patterns, and
run a **same-day** long-strangle backtest (buy combined premium near open,
sell near close) **excluding days where trade_date == expiry_date**.

Enrichment:
  - **Volume**: CE/PE volumes, per-bar ``vol_diff = CE_vol − PE_vol``, correlations with premium.
  - **ATR / range** (from ``nifty_daily``): prior-day **ATR**, trade-day **High−Low**,
    ratio **range / ATR** to tag expansion vs compression days.
  - **Short strangle** (sell strangle): same-day P&L = ``entry_open − exit_close`` (premium collected
    minus buyback) = **−long P&L** in points; optional **filters** using ``range/ATR``.

Outputs CSV + Markdown under ``output/strangle_backtest/`` (by default).

P&L convention (Nifty options, 1 lot per leg):
  points_pnl = (exit_total_premium - entry_total_premium) * LOT_SIZE
  (one lot each on CE and PE → scale by LOT_SIZE once for the combined strangle debit change).

This is a research tool, not live trading advice.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from nifty_algo.data.strangle_history_db import get_nifty_daily_row_before
from nifty_algo.data.strangle_history_db import get_nifty_daily_row_for_trade_date
from nifty_algo.data.strangle_history_db import get_strangle_db_path


LOT_SIZE = 50  # Nifty lot (adjust if your broker uses different)

# Heuristic filters (tune after viewing your month’s distribution)
LONG_RANGE_ATR_MIN = 1.0   # long strangle only if day range ≥ ATR × this (expansion vs prior ATR)
SHORT_RANGE_ATR_MAX = 0.92  # short strangle only if day range ≤ ATR × this (compression)


@dataclass(frozen=True)
class SessionKey:
    trade_date: str
    expiry_date: str
    ce_strike: int
    pe_strike: int


def _find_project_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").is_file() or (p / "src" / "nifty_algo").is_dir():
            return p
    return Path.cwd()


def load_month_frame(conn: sqlite3.Connection, month_prefix: str) -> pd.DataFrame:
    q = """
    SELECT trade_date, expiry_date, option_side, strike, timestamp, open, high, low, close, volume
    FROM strangle_history
    WHERE trade_date LIKE ?
    ORDER BY trade_date, expiry_date, timestamp
    """
    return pd.read_sql_query(q, conn, params=(f"{month_prefix}%",))


def discover_pairs(df: pd.DataFrame) -> list[SessionKey]:
    """All (trade_date, expiry, ce, pe) with aligned 1m bars (same timestamps on both legs)."""
    ce = df[df["option_side"].str.upper() == "CE"].copy()
    pe = df[df["option_side"].str.upper() == "PE"].copy()
    keys: list[SessionKey] = []
    for (td, exp), gce in ce.groupby(["trade_date", "expiry_date"], sort=True):
        gpe = pe[(pe["trade_date"] == td) & (pe["expiry_date"] == exp)]
        if gpe.empty:
            continue
        for kc in gce["strike"].unique():
            for kp in gpe["strike"].unique():
                a = gce[gce["strike"] == kc][["timestamp", "close", "open"]].rename(
                    columns={"close": "ce_c", "open": "ce_o"}
                )
                b = gpe[gpe["strike"] == kp][["timestamp", "close", "open"]].rename(
                    columns={"close": "pe_c", "open": "pe_o"}
                )
                m = a.merge(b, on="timestamp", how="inner")
                if len(m) < 50:
                    continue
                keys.append(SessionKey(str(td), str(exp), int(kc), int(kp)))
    # de-dupe
    seen = set()
    out: list[SessionKey] = []
    for k in keys:
        t = (k.trade_date, k.expiry_date, k.ce_strike, k.pe_strike)
        if t not in seen:
            seen.add(t)
            out.append(k)
    return out


def pick_session_pair(
    candidates: list[SessionKey],
    trade_date: str,
    *,
    skip_expiry_day: bool,
) -> SessionKey | None:
    """
    Prefer a non-expiry session for this trade_date; if multiple pairs, pick
    highest (CE_open+PE_open) at first bar as liquidity / ATM proxy.
    """
    day = [c for c in candidates if c.trade_date == trade_date]
    if not day:
        return None
    if skip_expiry_day:
        day = [c for c in day if c.trade_date != c.expiry_date]
    if not day:
        return None
    # prefer later expiry when same day has multiple (e.g. roll to next week)
    day_sorted = sorted(day, key=lambda x: x.expiry_date, reverse=True)
    # take first expiry group with most pairs? — take max expiry for this trade_date
    max_exp = max(d.expiry_date for d in day_sorted)
    day = [c for c in day_sorted if c.expiry_date == max_exp]
    if len(day) == 1:
        return day[0]
    # multiple strikes: pick by max first-bar premium (need df — caller passes)
    return day[0]


def merge_premium_vol(conn: sqlite3.Connection, k: SessionKey) -> pd.DataFrame:
    q = """
    SELECT timestamp, close, open, volume FROM strangle_history
    WHERE trade_date = ? AND expiry_date = ? AND option_side = ? AND strike = ?
    ORDER BY timestamp
    """
    ce = pd.read_sql_query(
        q, conn, params=(k.trade_date, k.expiry_date, "CE", k.ce_strike)
    )
    pe = pd.read_sql_query(
        q, conn, params=(k.trade_date, k.expiry_date, "PE", k.pe_strike)
    )
    m = ce.merge(pe, on="timestamp", suffixes=("_ce", "_pe"))
    m["total_close"] = m["close_ce"] + m["close_pe"]
    m["total_open"] = m["open_ce"] + m["open_pe"]
    m["volume_ce"] = pd.to_numeric(m["volume_ce"], errors="coerce").fillna(0.0)
    m["volume_pe"] = pd.to_numeric(m["volume_pe"], errors="coerce").fillna(0.0)
    m["vol_diff"] = m["volume_ce"] - m["volume_pe"]
    return m


# Alias for call sites that only need OHLC merge
merge_premium = merge_premium_vol


def run_backtest(
    conn: sqlite3.Connection,
    month_prefix: str,
    *,
    skip_expiry_day: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    df = load_month_frame(conn, month_prefix)
    if df.empty:
        raise SystemExit(f"No rows in strangle_history for trade_date LIKE {month_prefix!r}")

    pairs = discover_pairs(df)
    trade_days = sorted({p.trade_date for p in pairs})

    rows: list[dict[str, Any]] = []
    daily_stats: list[dict[str, Any]] = []

    for td in trade_days:
        chosen = pick_session_pair(pairs, td, skip_expiry_day=skip_expiry_day)
        if chosen is None:
            continue
        # resolve tie: max open premium among same (td, exp) candidates
        same_exp = [p for p in pairs if p.trade_date == td and p.expiry_date == chosen.expiry_date]
        if len(same_exp) > 1:
            best = None
            best_open = -1.0
            for k in same_exp:
                m = merge_premium(conn, k)
                if m.empty:
                    continue
                o = float(m.iloc[0]["total_open"])
                if o > best_open:
                    best_open = o
                    best = k
            if best is not None:
                chosen = best

        m = merge_premium_vol(conn, chosen)
        if m.empty or len(m) < 10:
            continue

        entry = m.iloc[0]
        exit_ = m.iloc[-1]
        hi = float(m["total_close"].max())
        lo = float(m["total_close"].min())
        entry_p = float(entry["total_open"])
        exit_p = float(exit_["total_close"])
        entry_c = float(entry["total_close"])
        pnl_pts = exit_p - entry_p
        pnl_inr = pnl_pts * LOT_SIZE
        # Short strangle (sell open, buy back close): P&L = credit − debit
        pnl_short_pts = entry_p - exit_p
        pnl_short_inr = pnl_short_pts * LOT_SIZE

        vd = m["vol_diff"]
        median_abs_vol_diff = float(vd.abs().median())
        mean_vol_diff = float(vd.mean())
        sum_ce_vol = float(m["volume_ce"].sum())
        sum_pe_vol = float(m["volume_pe"].sum())
        corr_vol_prem = float(vd.corr(m["total_close"])) if len(m) > 5 else float("nan")
        dpc = m["total_close"].diff()
        dvd = vd.diff()
        corr_volchg_premchg = float(dvd.corr(dpc)) if len(m) > 5 else float("nan")

        td_d = date.fromisoformat(chosen.trade_date)
        prev_nd = get_nifty_daily_row_before(td_d)
        day_nd = get_nifty_daily_row_for_trade_date(td_d)
        prev_atr = float(prev_nd["atr"]) if prev_nd and prev_nd.get("atr") is not None else float("nan")
        day_range = float(day_nd["range"]) if day_nd and day_nd.get("range") is not None else float("nan")
        range_ratio = (
            day_range / prev_atr
            if prev_atr == prev_atr and prev_atr > 0 and day_range == day_range
            else float("nan")
        )

        rows.append(
            {
                "trade_date": chosen.trade_date,
                "expiry_date": chosen.expiry_date,
                "ce_strike": chosen.ce_strike,
                "pe_strike": chosen.pe_strike,
                "entry_open_premium": entry_p,
                "entry_close_premium": entry_c,
                "exit_close_premium": exit_p,
                "day_high_premium": hi,
                "day_low_premium": lo,
                "pnl_points": pnl_pts,
                "pnl_inr": pnl_inr,
                "pnl_short_points": pnl_short_pts,
                "pnl_short_inr": pnl_short_inr,
                "median_abs_vol_diff": median_abs_vol_diff,
                "mean_vol_diff": mean_vol_diff,
                "sum_ce_volume": sum_ce_vol,
                "sum_pe_volume": sum_pe_vol,
                "corr_vol_diff_vs_premium": corr_vol_prem,
                "corr_volchg_vs_premchg": corr_volchg_premchg,
                "prev_day_atr": prev_atr,
                "nifty_day_range": day_range,
                "range_div_atr": range_ratio,
                "skipped_expiry_day": False,
            }
        )

        daily_stats.append(
            {
                "trade_date": chosen.trade_date,
                "expiry_date": chosen.expiry_date,
                "open_premium": entry_p,
                "close_premium": exit_p,
                "range_premium": hi - lo,
                "pnl_points": pnl_pts,
            }
        )

    trades = pd.DataFrame(rows)
    if trades.empty:
        return trades, pd.DataFrame(), {}

    trades["month"] = pd.to_datetime(trades["trade_date"]).dt.to_period("M").astype(str)

    monthly = (
        trades.groupby("month", as_index=False)
        .agg(
            sessions=("trade_date", "count"),
            pnl_inr_sum=("pnl_inr", "sum"),
            pnl_points_sum=("pnl_points", "sum"),
            win_rate=("pnl_inr", lambda s: float((s > 0).mean()) if len(s) else 0.0),
        )
    )

    monthly_short = (
        trades.groupby("month", as_index=False)
        .agg(
            sessions=("trade_date", "count"),
            pnl_short_inr_sum=("pnl_short_inr", "sum"),
            win_rate_short=("pnl_short_inr", lambda s: float((s > 0).mean()) if len(s) else 0.0),
        )
    )

    # --- Volume / ATR strategy grid (same-day hold)
    vol_med = trades["median_abs_vol_diff"].median()
    trades["vol_active"] = trades["median_abs_vol_diff"] >= vol_med
    has_rr = trades["range_div_atr"].notna() & (trades["range_div_atr"] > 0)
    mask_long_expand = has_rr & (trades["range_div_atr"] >= LONG_RANGE_ATR_MIN)
    mask_long_expand_vol = mask_long_expand & trades["vol_active"]
    mask_short_compress = has_rr & (trades["range_div_atr"] <= SHORT_RANGE_ATR_MAX)
    mask_short_compress_vol = mask_short_compress & trades["vol_active"]

    def _sum_win(col: str, mask: pd.Series) -> dict[str, Any]:
        sub = trades.loc[mask, col]
        if sub.empty:
            return {"n": 0, "pnl_inr": 0.0, "win_rate": 0.0}
        return {
            "n": int(mask.sum()),
            "pnl_inr": float(sub.sum()),
            "win_rate": float((sub > 0).mean()),
        }

    strategy_rows = [
        {"name": "Long -- all sessions", **_sum_win("pnl_inr", pd.Series(True, index=trades.index))},
        {
            "name": f"Long -- range/ATR >= {LONG_RANGE_ATR_MIN} (expansion vs prior ATR)",
            **_sum_win("pnl_inr", mask_long_expand),
        },
        {
            "name": "Long -- expansion + vol >= median |CE-PE| vol diff",
            **_sum_win("pnl_inr", mask_long_expand_vol),
        },
        {"name": "Short (sell strangle) -- all sessions", **_sum_win("pnl_short_inr", pd.Series(True, index=trades.index))},
        {
            "name": f"Short -- range/ATR <= {SHORT_RANGE_ATR_MAX} (compression)",
            **_sum_win("pnl_short_inr", mask_short_compress),
        },
        {
            "name": "Short -- compression + vol >= median |vol diff|",
            **_sum_win("pnl_short_inr", mask_short_compress_vol),
        },
    ]
    strategies_df = pd.DataFrame(strategy_rows)

    # Pattern summary
    try:
        terc = pd.qcut(trades["entry_open_premium"], 3, labels=["low", "mid", "high"], duplicates="drop")
        trades["_open_bucket"] = terc
        pnl_by_bucket = trades.groupby("_open_bucket", dropna=True, observed=True)["pnl_points"].agg(
            ["mean", "count"]
        )
        pnl_tercile = pnl_by_bucket.to_string()
    except (ValueError, TypeError):
        pnl_tercile = "(not enough distinct opens for terciles)"

    patterns: dict[str, Any] = {
        "sessions": int(len(trades)),
        "mean_pnl_points": float(trades["pnl_points"].mean()),
        "median_pnl_points": float(trades["pnl_points"].median()),
        "win_rate": float((trades["pnl_points"] > 0).mean()),
        "pnl_by_open_tercile": pnl_tercile,
        "correlation_open_vs_pnl": float(trades["entry_open_premium"].corr(trades["pnl_points"]))
        if len(trades) > 2
        else None,
        "corr_median_abs_vol_diff_vs_long_pnl": float(trades["median_abs_vol_diff"].corr(trades["pnl_points"]))
        if len(trades) > 2
        else None,
        "corr_range_ratio_vs_long_pnl": float(trades["range_div_atr"].corr(trades["pnl_points"]))
        if len(trades) > 2
        else None,
        "strategies_table": strategies_df.to_string(index=False),
        "strategies_df": strategies_df,
        "monthly_short": monthly_short,
    }

    return trades, monthly, patterns


def _df_to_md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:
        return "```\n" + df.to_string(index=False) + "\n```"


def write_report(
    out_dir: Path,
    month_prefix: str,
    trades: pd.DataFrame,
    monthly: pd.DataFrame,
    patterns: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out_dir / "trades.csv", index=False)
    monthly.to_csv(out_dir / "monthly_summary.csv", index=False)
    ms = patterns.get("monthly_short")
    if isinstance(ms, pd.DataFrame) and not ms.empty:
        ms.to_csv(out_dir / "monthly_summary_short_strangle.csv", index=False)
    sdf = patterns.get("strategies_df")
    if isinstance(sdf, pd.DataFrame) and not sdf.empty:
        sdf.to_csv(out_dir / "strategies.csv", index=False)
        (out_dir / "strategies.txt").write_text(sdf.to_string(index=False), encoding="utf-8")
        strat_txt = sdf.to_string(index=False)
    else:
        strat_txt = str(patterns.get("strategies_table", ""))

    lines = [
        f"# Strangle backtest — trade_date LIKE `{month_prefix}%`",
        "",
        "## Rules",
        "",
        "- **Long strangle**: entry at **first bar combined premium** (CE open + PE open), exit at **last bar combined close**.",
        "- **Short strangle (sell)**: same prices — P&L = **entry open − exit close** (credit minus buyback) = opposite of long in points.",
        "- **No trades on expiry day**: sessions where `trade_date == expiry_date` are **excluded**.",
        "- If multiple CE/PE pairs exist for a day, pick the **latest expiry** for that date, then the pair with **highest first-bar open premium** (ATM/liquidity proxy).",
        f"- P&L: `× {LOT_SIZE}` INR per combined strangle (1 lot CE + 1 lot PE; notional model).",
        "",
        "## Monthly P&L — long strangle",
        "",
        _df_to_md(monthly) if not monthly.empty else "_No data_",
        "",
        "## Monthly P&L — short (sell) strangle (all sessions)",
        "",
        _df_to_md(ms) if isinstance(ms, pd.DataFrame) and not ms.empty else "_No data_",
        "",
        "## Volume + ATR filters (research)",
        "",
        "- **Volume**: per minute `vol_diff = CE_volume − PE_volume`; session feature = **median(|vol_diff|)**. “Vol active” = above **median** of that column for the month.",
        "- **ATR**: **prev-day ATR** from `nifty_daily` (row before trade date). **Day range** = High−Low for **trade date** from `nifty_daily`. **range/ATR** compares realized cash range to prior ATR.",
        f"- **Long filter**: consider entries when **range/ATR >= {LONG_RANGE_ATR_MIN}** (expansion vs prior ATR).",
        f"- **Short (sell) filter**: consider when **range/ATR <= {SHORT_RANGE_ATR_MAX}** (compression -- short vol friendly in idealized backtest).",
        "",
        "### Strategy grid (sum of INR P&L over filtered sessions)",
        "",
        "```",
        str(strat_txt),
        "```",
        "",
        "## Pattern metrics (same-day hold)",
        "",
        f"- Sessions: **{patterns.get('sessions', 0)}**",
        f"- Mean P&L long (points): **{patterns.get('mean_pnl_points', 0):.2f}**",
        f"- Median P&L long (points): **{patterns.get('median_pnl_points', 0):.2f}**",
        f"- Win rate long (days > 0): **{patterns.get('win_rate', 0):.1%}**",
        f"- Corr(entry open premium, long P&L points): **{patterns.get('correlation_open_vs_pnl')}**",
        f"- Corr(median |vol_diff|, long P&L points): **{patterns.get('corr_median_abs_vol_diff_vs_long_pnl')}**",
        f"- Corr(range/ATR, long P&L points): **{patterns.get('corr_range_ratio_vs_long_pnl')}**",
        "",
        "### P&L by tercile of entry open premium (same month)",
        "",
        "```",
        str(patterns.get("pnl_by_open_tercile", "")),
        "```",
        "",
        "### Interpretation (heuristic)",
        "",
        "- **Long**: Favour **expansion** days (high **range/ATR**) and enough **two-sided flow** (median |vol diff|).",
        "- **Short (sell strangle)**: In idealized same-day terms, **compression** (low **range/ATR**) often aligns with **positive short-strangle** P&L if premium decays — validate on `strategies` table and your costs.",
        "- **When to buy (enter)**: Prefer entries when combined premium is **cheap vs recent range** (e.g. lower tercile of day-open premium) *if* your stats show positive mean P&L in that bucket — see `pnl_by_open_tercile` in the run log.",
        "- **When to sell (exit)**: Same-day model exits at **close**; intraday, consider taking profit when combined premium **reverts toward mean** after a spike, or **cut** if premium bleeds with low realized Nifty range.",
        "- **Expiry**: do **not** initiate or hold a discretionary strangle into **expiry day** (gamma/pin risk); this backtest already skips `trade_date == expiry_date`.",
        "",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Strangle DB pattern + monthly backtest")
    ap.add_argument(
        "--month",
        default="2025-12",
        help="Trade month prefix (YYYY-MM), default 2025-12",
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to strangle_data.db (default: project db/)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: output/strangle_backtest/<month>/)",
    )
    args = ap.parse_args()
    db_path = args.db or get_strangle_db_path()
    out_dir = args.out or (_find_project_root() / "output" / "strangle_backtest" / args.month)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        trades, monthly, patterns = run_backtest(
            conn, args.month, skip_expiry_day=True
        )
    finally:
        conn.close()

    write_report(out_dir, args.month, trades, monthly, patterns)
    print(f"Wrote: {out_dir / 'REPORT.md'}")
    print(f"Wrote: {out_dir / 'trades.csv'} ({len(trades)} rows)")
    print(f"Wrote: {out_dir / 'monthly_summary.csv'}")
    print(f"Wrote: {out_dir / 'strategies.txt'}")
    print("Strategies:\n", patterns.get("strategies_table", ""))


if __name__ == "__main__":
    main()
