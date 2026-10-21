#!/usr/bin/env python3
"""
Compare Fyers (algo_trading_data.db / options_data) vs Breeze (strangle_data.db / strangle_history)
volume difference (CE - PE) at ~1-minute resolution.

Fyers stores tick-style rows with cumulative day volume; this script:
  - buckets ticks into calendar minutes,
  - takes end-of-minute cumulative (max volume in that minute),
  - forward-fills missing minutes per leg,
  - converts to incremental volume per minute,
  - aligns with Breeze 1m bar volumes and reports diff stats.

Usage:
  python analysis/compare_fyers_breeze_volume.py
  python analysis/compare_fyers_breeze_volume.py --trade-date 2026-03-20 --expiry 2026-03-24 --ce 23650 --pe 22700
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def project_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "pyproject.toml").is_file() or (p / "src" / "nifty_algo").is_dir():
            return p
    return Path.cwd()


def parse_fyers_dt(date_s: str, time_s: str) -> datetime:
    time_s = (time_s or "").strip()
    if len(time_s) == 5 and time_s.count(":") == 1:  # HH:MM
        time_s = time_s + ":00"
    return datetime.strptime(f"{date_s.strip()} {time_s}", "%Y-%m-%d %H:%M:%S")


def minute_key(dt: datetime) -> str:
    m = dt.replace(second=0, microsecond=0)
    return m.strftime("%Y-%m-%d %H:%M:%S")


def load_fyers_ticks(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    ce_strike: int,
    pe_strike: int,
) -> tuple[list[tuple[datetime, float, str]], list[tuple[datetime, float, str]]]:
    """Returns (ce_ticks, pe_ticks) as (dt, cumulative_volume, symbol)."""
    rows = conn.execute(
        """
        SELECT DISTINCT symbol, strike, option_type
        FROM options_data
        WHERE date = ? AND strike IN (?, ?)
        """,
        (trade_date, ce_strike, pe_strike),
    ).fetchall()
    ce_sym = [r[0] for r in rows if int(r[1]) == ce_strike and str(r[2]).upper() == "CE"]
    pe_sym = [r[0] for r in rows if int(r[1]) == pe_strike and str(r[2]).upper() == "PE"]
    if len(ce_sym) != 1 or len(pe_sym) != 1:
        raise SystemExit(
            f"Expected exactly one CE and one PE symbol for strikes; got CE={ce_sym!r} PE={pe_sym!r}"
        )
    ce_sym, pe_sym = ce_sym[0], pe_sym[0]

    def leg(sym: str, strike: int, side: str) -> list[tuple[datetime, float, str]]:
        out: list[tuple[datetime, float, str]] = []
        for r in conn.execute(
            """
            SELECT timestamp, volume FROM options_data
            WHERE date = ? AND symbol = ? AND strike = ? AND UPPER(option_type) = ?
            ORDER BY timestamp
            """,
            (trade_date, sym, strike, side),
        ):
            try:
                dt = parse_fyers_dt(trade_date, r["timestamp"])
            except ValueError:
                continue
            out.append((dt, float(r["volume"] or 0), sym))
        return out

    return leg(ce_sym, ce_strike, "CE"), leg(pe_sym, pe_strike, "PE")


def _minute_range_union(ce_ticks: list[tuple[datetime, float, str]], pe_ticks: list[tuple[datetime, float, str]]) -> list[str]:
    """Shared 1-minute grid from earliest to latest tick across both legs."""
    times: list[datetime] = []
    for lst in (ce_ticks, pe_ticks):
        for dt, _, _ in lst:
            times.append(dt.replace(second=0, microsecond=0))
    if not times:
        return []
    t0 = min(times)
    t1 = max(times)
    out: list[str] = []
    cur = t0
    while cur <= t1:
        out.append(cur.strftime("%Y-%m-%d %H:%M:%S"))
        cur += timedelta(minutes=1)
    return out


def cumulative_to_minute_incremental(
    ticks: list[tuple[datetime, float, str]],
    all_minutes: list[str],
) -> dict[str, float]:
    """
    Bucket cumulative volumes into end-of-minute cumulative, forward-fill over all_minutes, incremental.
    """
    if not all_minutes:
        return {}

    by_min_end: dict[str, float] = defaultdict(float)
    for dt, vol, _ in ticks:
        mk = minute_key(dt)
        by_min_end[mk] = max(by_min_end[mk], vol)

    last = 0.0
    cum_ff: dict[str, float] = {}
    for mk in all_minutes:
        if mk in by_min_end:
            last = by_min_end[mk]
        cum_ff[mk] = last

    prev = 0.0
    inc: dict[str, float] = {}
    for mk in all_minutes:
        c = cum_ff[mk]
        inc[mk] = c - prev
        prev = c
    return inc


def load_breeze_minute_volumes(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    expiry_date: str,
    ce_strike: int,
    pe_strike: int,
) -> tuple[dict[str, float], dict[str, float]]:
    ce: dict[str, float] = {}
    pe: dict[str, float] = {}
    for side, strike, target in [("CE", ce_strike, ce), ("PE", pe_strike, pe)]:
        for r in conn.execute(
            """
            SELECT timestamp, volume FROM strangle_history
            WHERE trade_date = ? AND expiry_date = ? AND strike = ? AND option_side = ?
            ORDER BY timestamp
            """,
            (trade_date, expiry_date, strike, side),
        ):
            ts = str(r["timestamp"]).strip().replace("T", " ")
            if len(ts) >= 19:
                ts = ts[:19]
            target[ts] = float(r["volume"] or 0)
    return ce, pe


def vol_diff_crores(ce: dict[str, float], pe: dict[str, float]) -> dict[str, float]:
    keys = sorted(set(ce) | set(pe))
    out: dict[str, float] = {}
    for k in keys:
        out[k] = (ce.get(k, 0.0) - pe.get(k, 0.0)) / 1e7
    return out


def main() -> None:
    root = project_root()
    ap = argparse.ArgumentParser(description="Compare Fyers vs Breeze 1m volume diff (CE-PE)")
    ap.add_argument("--trade-date", default="2026-03-20")
    ap.add_argument("--expiry", default="2026-03-24")
    ap.add_argument("--ce", type=int, default=23650, dest="ce_strike")
    ap.add_argument("--pe", type=int, default=22700, dest="pe_strike")
    ap.add_argument(
        "--fyers-db",
        type=Path,
        default=root / "algo_logs" / "algo_trading_data.db",
    )
    ap.add_argument(
        "--breeze-db",
        type=Path,
        default=root / "db" / "strangle_data.db",
    )
    ap.add_argument("--csv", type=Path, default=None, help="Write per-minute comparison CSV")
    args = ap.parse_args()

    trade_date = args.trade_date
    expiry = args.expiry

    if not args.fyers_db.is_file():
        raise SystemExit(f"Fyers DB not found: {args.fyers_db}")
    if not args.breeze_db.is_file():
        raise SystemExit(f"Breeze DB not found: {args.breeze_db}")

    cfy = sqlite3.connect(str(args.fyers_db))
    cfy.row_factory = sqlite3.Row
    cbz = sqlite3.connect(str(args.breeze_db))
    cbz.row_factory = sqlite3.Row

    try:
        ce_ticks, pe_ticks = load_fyers_ticks(
            cfy,
            trade_date=trade_date,
            ce_strike=args.ce_strike,
            pe_strike=args.pe_strike,
        )
        fy_minutes = _minute_range_union(ce_ticks, pe_ticks)
        fy_ce_inc = cumulative_to_minute_incremental(ce_ticks, fy_minutes)
        fy_pe_inc = cumulative_to_minute_incremental(pe_ticks, fy_minutes)

        bz_ce, bz_pe = load_breeze_minute_volumes(
            cbz,
            trade_date=trade_date,
            expiry_date=expiry,
            ce_strike=args.ce_strike,
            pe_strike=args.pe_strike,
        )

        fy_diff = vol_diff_crores(fy_ce_inc, fy_pe_inc)
        bz_diff = vol_diff_crores(bz_ce, bz_pe)

        common = sorted(set(fy_diff) & set(bz_diff))
        only_fy = sorted(set(fy_diff) - set(bz_diff))
        only_bz = sorted(set(bz_diff) - set(fy_diff))

        print("=== compare_fyers_breeze_volume ===")
        print(f"Trade date: {trade_date}  Expiry: {expiry}  CE={args.ce_strike} PE={args.pe_strike}")
        print(f"Fyers DB: {args.fyers_db}")
        print(f"Breeze DB: {args.breeze_db}")
        print()
        print(f"Fyers CE ticks: {len(ce_ticks)}  PE ticks: {len(pe_ticks)}")
        print(f"Fyers minute buckets (incremental): CE={len(fy_ce_inc)} PE={len(fy_pe_inc)}")
        print(f"Breeze 1m bars: CE={len(bz_ce)} PE={len(bz_pe)}")
        print()
        print(f"Aligned minutes (intersection): {len(common)}")
        print(f"Only Fyers (no Breeze bar): {len(only_fy)}")
        print(f"Only Breeze (no Fyers bucket): {len(only_bz)}")
        print()

        if common:
            deltas = [fy_diff[t] - bz_diff[t] for t in common]
            abs_d = [abs(x) for x in deltas]
            print("Per-minute (CE-PE) vol diff in CRORES -- FY vs BZ delta (FY - BZ):")
            print(f"  mean(abs): {statistics.mean(abs_d):.6f}  max(abs): {max(abs_d):.6f}")
            print(f"  mean signed: {statistics.mean(deltas):.6f}  stdev: {statistics.pstdev(deltas):.6f}")
            worst = sorted([(abs(fy_diff[t] - bz_diff[t]), t, fy_diff[t], bz_diff[t]) for t in common], reverse=True)[:8]
            print("  largest |FY-BZ| (crores):")
            for ad, t, f, b in worst:
                print(f"    {t}  FY={f:+.6f}  BZ={b:+.6f}  d={f-b:+.6f}")

        if args.csv and common:
            args.csv.parent.mkdir(parents=True, exist_ok=True)
            with open(args.csv, "w", encoding="utf-8") as f:
                f.write("timestamp,fy_vol_diff_cr,bz_vol_diff_cr,fy_minus_bz_cr,fy_ce_inc_sh,fy_pe_inc_sh,bz_ce_vol_sh,bz_pe_vol_sh\n")
                for t in common:
                    f.write(
                        f"{t},{fy_diff[t]:.8f},{bz_diff[t]:.8f},{fy_diff[t]-bz_diff[t]:.8f},"
                        f"{fy_ce_inc.get(t,0):.2f},{fy_pe_inc.get(t,0):.2f},"
                        f"{bz_ce.get(t,0):.2f},{bz_pe.get(t,0):.2f}\n"
                    )
            print(f"\nWrote {args.csv}")
    finally:
        cfy.close()
        cbz.close()


if __name__ == "__main__":
    main()
