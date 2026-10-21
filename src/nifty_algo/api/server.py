"""
REST API server for reading market data from cache
Run with: uvicorn nifty_algo.api.server:app --reload --port 8000
"""

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date, datetime, time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nifty_algo.cache.market_cache import get_cache

logger = logging.getLogger(__name__)


def _volume_chart_display_for_live() -> dict[str, Any]:
    """
    Same scaling as backtest_ui: Crores (÷1e7) when today is expiry day, else Lakhs (÷1e5).
    Raw CE/PE volumes are in shares; diff is CE − PE.
    """
    from nifty_algo.market.expiry import get_nearest_expiry

    today = date.today()
    exp = get_nearest_expiry(None)
    if exp is not None and today == exp:
        return {"unit": "Crores", "divisor": 1e7}
    return {"unit": "Lakhs", "divisor": 1e5}


def _calc_strangle_strikes_from_spot(spot: float) -> tuple[int, int]:
    """Compute display strangle strikes using same ATM+offset rule as tracker/backtest."""
    from nifty_algo.config import config

    interval = int(getattr(config, "STRIKE_INTERVAL", 50) or 50)
    offset = int(getattr(config, "STRANGLE_ATM_OFFSET_POINTS", 100) or 100)
    atm = int(round(float(spot) / interval) * interval)
    return atm + offset, atm - offset


def _api_project_root() -> Path:
    """Repo root (pyproject.toml) so LOG_DIR=algo_logs resolves like the algo process."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def _db_path_for_live_snapshots() -> Path:
    from nifty_algo.config import config

    log_dir = Path(config.LOG_DIR)
    if not log_dir.is_absolute():
        log_dir = (_api_project_root() / log_dir).resolve()
    return (log_dir / config.DATABASE_NAME).resolve()


def _parse_api_dt(ts: str) -> datetime:
    """Parse common timestamp formats stored in live snapshot DB."""
    s = str(ts).strip()
    if not s:
        raise ValueError("empty timestamp")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported timestamp format: {ts!r}")


def _parse_snapshot_datetime(trade_date: str, ts_cell: str) -> datetime:
    """
    DB snapshot rows use `date` + `timestamp` where timestamp is often **time-only**
    (%H:%M:%S) from MarketCache / main — datetime.fromisoformat cannot parse that alone.
    """
    s = str(ts_cell).strip()
    if not s:
        raise ValueError("empty timestamp")
    if len(s) >= 10 and ("T" in s or " " in s):
        return _parse_api_dt(s.replace(" ", "T", 1) if " " in s and "T" not in s else s)
    td = date.fromisoformat(trade_date)
    for fmt in ("%H:%M:%S", "%H:%M:%S.%f"):
        try:
            tpart = datetime.strptime(s, fmt).time()
            return datetime.combine(td, tpart)
        except ValueError:
            continue
    raise ValueError(f"Unsupported snapshot time {ts_cell!r} for {trade_date}")


def _chart_timestamp_iso(trade_date: str, ts_cell: str) -> str:
    """ISO-like string Chart.js / Date() parse reliably across browsers."""
    return _parse_snapshot_datetime(trade_date, ts_cell).isoformat(sep="T", timespec="seconds")


def _build_historical_history_from_db(trade_date: str, limit: int) -> dict[str, Any]:
    """
    Build strangle chart history for one trade date from live DB snapshots.
    Uses display strikes derived from first spot row of that date.

    ``limit <= 0`` returns the full aligned series for the day. ``limit > 0`` caps the count
    **from session open** (prefix), never the tail-only slice that hid pre-noon bars.
    """
    db_path = _db_path_for_live_snapshots()
    if not db_path.exists():
        return {
            "status": "no_data",
            "message": f"Live DB not found at {db_path} (set LOG_DIR / DATABASE_NAME or run from repo root).",
            "history": [],
            "count": 0,
        }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        spot_rows = conn.execute(
            """
            SELECT timestamp, COALESCE(nifty_spot, spot_price) AS spot
            FROM nifty_spot
            WHERE date = ?
            ORDER BY timestamp ASC
            """,
            (trade_date,),
        ).fetchall()

        if not spot_rows:
            return {
                "status": "no_data",
                "message": f"No spot data found for {trade_date}",
                "history": [],
                "count": 0,
            }

        first_spot = next(
            (float(r["spot"]) for r in spot_rows if r["spot"] is not None and float(r["spot"]) > 0),
            None,
        )
        if first_spot is None:
            return {
                "status": "no_data",
                "message": f"Spot data invalid for {trade_date}",
                "history": [],
                "count": 0,
            }

        ce_strike, pe_strike = _calc_strangle_strikes_from_spot(first_spot)

        opt_cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(options_data)").fetchall()
        }
        has_volume_raw = "volume_raw" in opt_cols

        def load_leg_rows(strike: int, option_type: str) -> dict[str, dict[str, float]]:
            """
            Match StrangleTracker / live chart: ce_volume and pe_volume are broker **cumulative**
            contract volume (Volume), not per-snapshot delta.

            In options_data, `volume` stores incremental delta; `volume_raw` stores the broker
            cumulative at save time — use raw when present, else running sum of deltas.
            """
            side = option_type.strip().upper()
            strike_f = float(strike)
            vol_sel = "timestamp, ltp, volume, volume_raw" if has_volume_raw else "timestamp, ltp, volume"
            rows = conn.execute(
                f"""
                SELECT {vol_sel}
                FROM options_data
                WHERE date = ?
                  AND ABS(CAST(strike AS REAL) - ?) < 0.51
                  AND UPPER(TRIM(COALESCE(option_type, ''))) = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (trade_date, strike_f, side),
            ).fetchall()
            by_ts: dict[str, dict[str, float]] = {}
            cum_from_delta = 0.0
            for r in rows:
                ts = str(r["timestamp"])
                ltp = float(r["ltp"]) if r["ltp"] is not None else None
                delta = float(r["volume"]) if r["volume"] is not None else 0.0
                raw = None
                if has_volume_raw and r["volume_raw"] is not None:
                    raw = float(r["volume_raw"])
                if raw is not None and raw == raw:
                    vol_shares = raw
                    cum_from_delta = raw
                else:
                    cum_from_delta += delta
                    vol_shares = cum_from_delta
                by_ts[ts] = {"ltp": ltp, "vol_shares": vol_shares}
            return by_ts

        ce_by_ts = load_leg_rows(ce_strike, "CE")
        pe_by_ts = load_leg_rows(pe_strike, "PE")

        if not ce_by_ts or not pe_by_ts:
            return {
                "status": "no_data",
                "message": (
                    f"No complete CE/PE data for {trade_date} "
                    f"(CE {ce_strike}, PE {pe_strike})"
                ),
                "history": [],
                "count": 0,
            }

        spot_sorted: list[tuple[datetime, float]] = []
        for r in spot_rows:
            ts = str(r["timestamp"])
            sv = r["spot"]
            if sv is None:
                continue
            try:
                spot_sorted.append((_parse_snapshot_datetime(trade_date, ts), float(sv)))
            except (ValueError, TypeError):
                continue
        spot_sorted.sort(key=lambda x: x[0])
        if not spot_sorted:
            return {
                "status": "no_data",
                "message": (
                    f"Spot timestamps invalid for {trade_date} (check {db_path}; "
                    "expected time-only HH:MM:SS or full datetime in nifty_spot.timestamp)."
                ),
                "history": [],
                "count": 0,
            }

        history: list[dict[str, Any]] = []
        spot_idx = 0
        last_spot = spot_sorted[0][1]

        shared_ts = sorted(
            set(ce_by_ts.keys()) & set(pe_by_ts.keys()),
            key=lambda t: _parse_snapshot_datetime(trade_date, t),
        )
        for ts in shared_ts:
            ce = ce_by_ts.get(ts)
            pe = pe_by_ts.get(ts)
            if not ce or not pe:
                continue
            ce_price = ce.get("ltp")
            pe_price = pe.get("ltp")
            if ce_price is None or pe_price is None:
                continue
            tdt = _parse_snapshot_datetime(trade_date, ts)
            while spot_idx + 1 < len(spot_sorted) and spot_sorted[spot_idx + 1][0] <= tdt:
                spot_idx += 1
                last_spot = spot_sorted[spot_idx][1]

            ce_vol = float(ce.get("vol_shares", 0.0) or 0.0)
            pe_vol = float(pe.get("vol_shares", 0.0) or 0.0)
            history.append(
                {
                    "timestamp": _chart_timestamp_iso(trade_date, ts),
                    "ce_strike": ce_strike,
                    "pe_strike": pe_strike,
                    "ce_price": float(ce_price),
                    "pe_price": float(pe_price),
                    "ce_volume": ce_vol,
                    "pe_volume": pe_vol,
                    "volume_diff": ce_vol - pe_vol,
                    "total_premium": float(ce_price) + float(pe_price),
                    "spot_price": float(last_spot),
                }
            )

        # Keep earliest bars (session open). Old [-limit:] kept only the tail and hid 09:15–11:00.
        if limit > 0 and len(history) > limit:
            history = history[:limit]

        if not history:
            return {
                "status": "no_data",
                "message": f"No aligned CE/PE timestamps found for {trade_date}",
                "history": [],
                "count": 0,
            }

        from nifty_algo.config import config

        current = {
            "ce_strike": ce_strike,
            "pe_strike": pe_strike,
            "selected_at": history[0]["timestamp"],
            "ce_price": history[-1]["ce_price"],
            "pe_price": history[-1]["pe_price"],
            "ce_volume": history[-1]["ce_volume"],
            "pe_volume": history[-1]["pe_volume"],
            "volume_diff": history[-1]["volume_diff"],
            "total_premium": history[-1]["total_premium"],
            "spot_price": history[-1]["spot_price"],
            "max_premium_target": float(getattr(config, "MAX_TOTAL_PREMIUM", 112.0)),
            "ce_symbol": None,
            "pe_symbol": None,
        }
        return {
            "status": "ok",
            "current": current,
            "history": history,
            "count": len(history),
            "volume_chart": _volume_chart_display_for_live(),
        }
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init queue and broadcast worker. Shutdown: clear globals."""
    global _event_loop, _update_queue

    # Startup
    _event_loop = asyncio.get_running_loop()
    _update_queue = asyncio.Queue()
    cache = _get_active_cache()
    cache.subscribe(_enqueue_update)
    # If app.state.cache is a non-singleton instance while algo uses get_cache(), subscribe both.
    singleton = get_cache()
    if singleton is not cache:
        singleton.subscribe(_enqueue_update)
        logger.warning(
            "WebSocket subscribed to two MarketCache instances (ids %s vs %s). "
            "Chart updates only if publish_options hits that cache.",
            id(cache),
            id(singleton),
        )
    logger.info("Cache subscriber registered with async queue")
    asyncio.create_task(_broadcast_worker())
    logger.info("FastAPI startup complete")

    yield

    # Shutdown
    _event_loop = None
    _update_queue = None
    logger.info("FastAPI shutdown complete")


app = FastAPI(title="Nifty Options", version="1.0.0", lifespan=lifespan)
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# allow CORS for frontend/charts
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#------------------------
# Response Models
#------------------------
class SpotResponse(BaseModel):
    price: float | None
    timestamp: str | None

class OptionData(BaseModel):
    symbol: str | None
    strike: float | None
    option_type: str | None
    ltp: float | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    iv: float | None
    timestamp: str | None

class OptionsResponse(BaseModel):
    count: int
    data: list[dict[str, Any]]
    spot_price: float | None
    spot_timestamp: str | None

class HealthResponse(BaseModel):
    status: str
    cache_size: int
    spot_price: float | None

#-------------------------
# Rest Endpoints
#-------------------------
def _get_active_cache():
    """Get cache from app.state (set by main.py) or fallback to singleton"""
    if hasattr(app.state, 'cache') and app.state.cache is not None:
        return app.state.cache
    return get_cache()

@app.get("/health", response_model=HealthResponse)
def health_check():
    """Health check endpoint"""
    cache = _get_active_cache()
    spot, _ = cache.get_spot()
    return {
        "status": "ok",
        "cache_size": len(cache.get_latest_options()),
        "spot_price": spot,
    }

@app.get("/debug")
def debug_info():
    """Debug endpoint to check cache and strangle state"""
    app_state_cache = getattr(app.state, 'cache', None)
    local_cache = get_cache()
    cache = app_state_cache if app_state_cache else local_cache
    spot, ts = cache.get_spot()
    options = cache.get_latest_options(limit=300)
    has_ce = has_pe = False
    try:
        from nifty_algo.core.strangle_tracker import get_strangle_tracker
        tracker = get_strangle_tracker()
        if spot and spot > 0:
            tracker.update(options, spot, record_history=False)
        cur = tracker.get_current()
        if cur:
            ce_s, pe_s = cur.get("ce_strike"), cur.get("pe_strike")
            for o in options:
                s = o.get("strike")
                t = o.get("option_type")
                if s is not None and t:
                    try:
                        si = int(s)
                        if si == ce_s and t == "CE":
                            has_ce = True
                        if si == pe_s and t == "PE":
                            has_pe = True
                    except (TypeError, ValueError):
                        pass
    except Exception as e:
        cur = None
    return {
        "using_app_state_cache": app_state_cache is not None,
        "app_state_cache_id": id(app_state_cache) if app_state_cache is not None else None,
        "singleton_cache_id": id(local_cache),
        "active_cache_id": id(cache),
        "cache_size": len(cache._options_data),
        "spot_price": spot,
        "strangle_current": cur,
        "cache_has_ce_strike": has_ce,
        "cache_has_pe_strike": has_pe,
    }

@app.get("/spot", response_model=SpotResponse)
def get_spot():
    """Get current Nifty spot price"""
    cache = _get_active_cache()
    spot, ts = cache.get_spot()
    return {
        "price": spot,
        "timestamp": ts.isoformat() if ts else None
    }

@app.get("/options", response_model=OptionsResponse)
def get_options(limit: int = 100):
    """Get latest options data"""
    cache = _get_active_cache()
    data = cache.get_latest_options(limit)
    spot, ts = cache.get_spot()
    return {
        "count": len(data),
        "data": data,
        "spot_price": spot,
        "spot_timestamp": ts.isoformat() if ts else None
    }

@app.get("/options/by-strike/{strike}")
def get_options_by_strike(strike: float):
    """Get CE and PE data for a specific strike"""
    cache = _get_active_cache()
    all_data = cache.get_latest_options(limit=1000)

    ce_data = None
    pe_data = None

    for row in reversed(all_data):
        row_strike = row.get("strike")
        opt_type = row.get("option_type")

        if row_strike == strike:
            if opt_type == "CE" and ce_data is None:
                ce_data = row
            elif opt_type == "PE" and pe_data is None:
                pe_data = row

        if ce_data and pe_data:
            break
    
    spot, ts = cache.get_spot()
    return {
        "strike": strike,
        "CE": ce_data,
        "PE": pe_data,
        "spot_price": spot,
        "spot_timestamp": ts.isoformat() if ts else None
    }

@app.get("/options/chain")
def get_option_chain():
    """Get full option chain (latest data for each strike)."""
    cache = _get_active_cache()
    all_data = cache.get_latest_options(limit=1000)
    spot, spot_ts = cache.get_spot()
    
    chain: dict[int, dict[str, Any]] = {}
    
    for row in reversed(all_data):
        strike = row.get("strike")
        opt_type = row.get("option_type")
        
        if strike is None or opt_type is None:
            continue
        
        strike = int(strike)
        if strike not in chain:
            chain[strike] = {"CE": None, "PE": None}
        
        if chain[strike][opt_type] is None:
            chain[strike][opt_type] = row
    
    sorted_chain = dict(sorted(chain.items()))
    
    return {
        "chain": sorted_chain,
        "strikes": list(sorted_chain.keys()),
        "spot_price": spot,
        "spot_timestamp": spot_ts.isoformat() if spot_ts else None,
    }

#-------------------------
# Strangle Tracking (for Chart)
#-------------------------

@app.get("/strangle/current")
def get_strangle_current():
    """Get current strangle being tracked (different CE/PE strikes)."""
    from nifty_algo.core.strangle_tracker import get_strangle_tracker
    cache = _get_active_cache()
    spot, _ = cache.get_spot()
    options = cache.get_latest_options(limit=200)
    tracker = get_strangle_tracker()
    if spot is not None and spot > 0:
        tracker.update(options, spot, record_history=False)
    current = tracker.get_current()
    if current is None:
        return {"status": "no_strangle", "message": "No strangle selected yet"}
    return {"status": "ok", "strangle": current}

@app.get("/strangle/history")
def get_strangle_history(limit: int = 0):
    """Get strangle price history for charting. ``limit=0`` (default) returns full retained series from session open."""
    from nifty_algo.core.strangle_tracker import get_strangle_tracker
    cache = _get_active_cache()
    spot, _ = cache.get_spot()
    options = cache.get_latest_options(limit=200)
    tracker = get_strangle_tracker()
    if spot is not None and spot > 0:
        tracker.update(options, spot, record_history=False)
    history = tracker.get_history(limit)
    current = tracker.get_current()
    # Tracker is in-memory only: after restart or before first tick, history is empty
    # even when today's session was persisted to algo_trading_data.db — fill from DB.
    if not history:
        td = date.today().isoformat()
        db_pack = _build_historical_history_from_db(td, limit)
        if db_pack.get("status") == "ok" and db_pack.get("history"):
            history = db_pack["history"]
            if current is None or current.get("total_premium") is None:
                current = db_pack.get("current")
    return {
        "status": "ok",
        "current": current,
        "history": history,
        "count": len(history),
        "volume_chart": _volume_chart_display_for_live(),
    }


@app.get("/strangle/history/by-date")
def get_strangle_history_by_date(trade_date: str, limit: int = 0):
    """Get historical strangle chart points for a specific date (YYYY-MM-DD). ``limit=0`` = full day."""
    try:
        date.fromisoformat(trade_date)
    except ValueError:
        return {
            "status": "error",
            "message": "trade_date must be in YYYY-MM-DD format",
            "history": [],
            "count": 0,
        }
    return _build_historical_history_from_db(trade_date, max(0, int(limit)))


#-------------------------
# Nifty Chart (volume range + OTM + opening range)
#-------------------------

def _nifty_chart_snapshot_for_ws() -> tuple[Any, Any]:
    from nifty_algo.core.nifty_chart_tracker import get_nifty_chart_tracker

    tracker = get_nifty_chart_tracker()
    cur = tracker.get_current()
    history = tracker.get_history(-1)
    if history:
        return cur, history[-1]
    return cur, None


def _build_nifty_historical_history_from_db(
    trade_date: str,
    limit: int,
    tracker: Any = None,
) -> dict[str, Any]:
    """Rebuild nifty chart history from DB snapshots (volume range + offset strangle).

    When ``tracker`` is the live singleton, replay into it so later ticks continue
    the same volume state and the on-screen series matches the saved day.
    """
    from nifty_algo.core.nifty_chart_tracker import NiftyChartTracker
    from nifty_algo.core.volume_range import OR_END, OR_START

    db_path = _db_path_for_live_snapshots()
    if not db_path.exists():
        return {"status": "no_data", "message": f"Live DB not found at {db_path}", "history": [], "count": 0}

    from nifty_algo.config import config

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        spot_rows = conn.execute(
            """
            SELECT timestamp, COALESCE(nifty_spot, spot_price) AS spot
            FROM nifty_spot WHERE date = ? ORDER BY timestamp ASC
            """,
            (trade_date,),
        ).fetchall()
        if not spot_rows:
            return {"status": "no_data", "message": f"No spot for {trade_date}", "history": [], "count": 0}

        opt_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(options_data)").fetchall()}
        has_volume_raw = "volume_raw" in opt_cols
        vol_col = "volume_raw" if has_volume_raw else "volume"

        opt_rows = conn.execute(
            f"""
            SELECT timestamp, strike, option_type, ltp, {vol_col} AS vol
            FROM options_data WHERE date = ? ORDER BY timestamp ASC, id ASC
            """,
            (trade_date,),
        ).fetchall()
        if not opt_rows:
            return {"status": "no_data", "message": f"No options for {trade_date}", "history": [], "count": 0}

        spot_by_ts: dict[str, float] = {}
        for r in spot_rows:
            if r["spot"] is not None:
                spot_by_ts[str(r["timestamp"])] = float(r["spot"])

        # Group options by timestamp
        chain_by_ts: dict[str, list[dict]] = {}
        for r in opt_rows:
            ts = str(r["timestamp"])
            vol = r["vol"]
            chain_by_ts.setdefault(ts, []).append(
                {
                    "strike": r["strike"],
                    "option_type": r["option_type"],
                    "ltp": r["ltp"],
                    "LTP": r["ltp"],
                    "volume": vol,
                    "Volume": vol,
                }
            )

        if tracker is None:
            tracker = NiftyChartTracker(
                atm_offset_points=int(getattr(config, "STRANGLE_ATM_OFFSET_POINTS", 100)),
                strike_interval=int(getattr(config, "STRIKE_INTERVAL", 50)),
                lot_size=int(getattr(config, "ORDER_QTY", 75)),
            )
        tracker.reset()

        shared_ts = sorted(
            chain_by_ts.keys(),
            key=lambda t: _parse_snapshot_datetime(trade_date, t),
        )
        history: list[dict[str, Any]] = []
        or_high = or_low = None
        or_frozen = False

        for ts in shared_ts:
            chain = chain_by_ts[ts]
            tdt = _parse_snapshot_datetime(trade_date, ts)
            spot = spot_by_ts.get(ts)
            if spot is None:
                # nearest prior spot
                prior = [spot_by_ts[k] for k in spot_by_ts if _parse_snapshot_datetime(trade_date, k) <= tdt]
                spot = prior[-1] if prior else None
            if spot is None:
                continue

            tt = tdt.time().replace(microsecond=0)
            if not or_frozen and OR_START <= tt < OR_END:
                or_high = spot if or_high is None else max(or_high, spot)
                or_low = spot if or_low is None else min(or_low, spot)
            if tt >= OR_END:
                or_frozen = True

            snap = tracker.update(chain, spot, record_history=True, now=tdt)
            if snap:
                row = tracker.snapshot_to_dict(snap)
                row["timestamp"] = _chart_timestamp_iso(trade_date, ts)
                row["or_high"] = or_high if or_high and or_high > 0 else None
                row["or_low"] = or_low if or_low and or_low > 0 else None
                history.append(row)

        if limit > 0 and len(history) > limit:
            history = history[:limit]

        if not history:
            return {"status": "no_data", "message": "No aligned history", "history": [], "count": 0}

        current = history[-1]
        return {
            "status": "ok",
            "current": current,
            "history": history,
            "count": len(history),
            "volume_chart": _volume_chart_display_for_live(),
            "or_levels": {"or_high": or_high, "or_low": or_low},
        }
    finally:
        conn.close()


def _history_covers_session_open(history: list[dict[str, Any]]) -> bool:
    if not history:
        return False
    raw = str(history[0].get("timestamp") or "")
    try:
        ts = _parse_api_dt(raw)
    except Exception:
        return False
    return ts.time() <= time(9, 30)


def _ensure_live_nifty_session(tracker: Any) -> None:
    """Fill a late-started live tracker from today's DB so live matches after close."""
    if getattr(tracker, "_session_checked", False):
        return
    tracker._session_checked = True
    if _history_covers_session_open(tracker.get_history(1)):
        return
    _build_nifty_historical_history_from_db(date.today().isoformat(), 0, tracker=tracker)


@app.get("/niftychart/history")
def get_niftychart_history(limit: int = 0):
    from nifty_algo.core.nifty_chart_tracker import get_nifty_chart_tracker

    cache = _get_active_cache()
    spot, _ = cache.get_spot()
    options = cache.get_latest_options(limit=500)
    tracker = get_nifty_chart_tracker()
    _ensure_live_nifty_session(tracker)
    if spot is not None and spot > 0:
        tracker.update(options, spot, record_history=False)
    history = tracker.get_history(limit)
    current = tracker.get_current()
    or_levels = tracker.get_or_levels()
    if not history:
        td = date.today().isoformat()
        db_pack = _build_nifty_historical_history_from_db(td, limit)
        if db_pack.get("status") == "ok" and db_pack.get("history"):
            history = db_pack["history"]
            current = db_pack.get("current")
            or_levels = db_pack.get("or_levels", or_levels)
    return {
        "status": "ok",
        "current": current,
        "history": history,
        "count": len(history),
        "volume_chart": _volume_chart_display_for_live(),
        "or_levels": or_levels,
    }


def _expiry_for_historical_trade_date(trade_date: date) -> date:
    """Prefer NIFTY_EXPIRY_DATE when it covers this session; else next weekly weekday on/after trade_date."""
    from nifty_algo.config import config
    from nifty_algo.market.expiry import next_weekly_expiry_on_or_after

    env_exp = config.NIFTY_EXPIRY_DATE
    if env_exp is not None and env_exp >= trade_date:
        return env_exp
    return next_weekly_expiry_on_or_after(trade_date, config.NIFTY_WEEKLY_EXPIRY_WEEKDAY)


def _strangle_legs_available_for_trade_date(trade_date: date) -> list[tuple[date, int, int]]:
    """Return [(expiry, ce_strike, pe_strike), ...] present in strangle_history for trade_date."""
    from nifty_algo.data.strangle_history_db import get_strangle_db_path

    path = get_strangle_db_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT expiry_date, option_side, strike
            FROM strangle_history
            WHERE trade_date = ?
            GROUP BY expiry_date, option_side, strike
            """,
            (trade_date.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    by_exp: dict[str, dict[str, set[int]]] = {}
    for exp_s, side, strike in rows:
        by_exp.setdefault(str(exp_s), {}).setdefault(str(side).upper(), set()).add(int(strike))
    out: list[tuple[date, int, int]] = []
    for exp_s, sides in by_exp.items():
        ces = sorted(sides.get("CE") or [])
        pes = sorted(sides.get("PE") or [])
        if not ces or not pes:
            continue
        try:
            exp_d = date.fromisoformat(exp_s)
        except ValueError:
            continue
        # Prefer single CE/PE pair closest to ATM±offset when multiple exist
        if len(ces) == 1 and len(pes) == 1:
            out.append((exp_d, ces[0], pes[0]))
        else:
            out.append((exp_d, ces[0], pes[0]))
    return out


def _build_nifty_historical_history_from_strangle_db(
    trade_date: str, limit: int
) -> dict[str, Any]:
    """
    Rebuild /niftychart history from backtest cache (db/strangle_data.db):
    nifty_index_history + strangle_history CE/PE 1m bars.

    Volume-range / OTM layers need a full chain (live snapshots only); offset strangle
    + Nifty + OR still chart from this source. Optionally downloads via HISTORICAL_BROKER
    when cache misses.
    """
    from nifty_algo.brokers import get_historical_client
    from nifty_algo.config import config
    from nifty_algo.core.nifty_chart_tracker import NiftyChartTracker
    from nifty_algo.core.volume_range import OR_END, OR_START
    from nifty_algo.data.strangle_history_db import get_nifty_index_history_from_db
    from nifty_algo.data.strangle_history_db import get_strangle_history_from_db

    td = date.fromisoformat(trade_date)
    interval = int(getattr(config, "STRIKE_INTERVAL", 50) or 50)
    offset = int(getattr(config, "STRANGLE_ATM_OFFSET_POINTS", 100) or 100)

    nifty_bars = get_nifty_index_history_from_db(trade_date=td)
    hist_name = (getattr(config, "HISTORICAL_BROKER", None) or "breeze").strip().lower()
    downloaded = False

    if not nifty_bars:
        try:
            client = get_historical_client(config)
            if client.connect():
                nifty_bars, _src = client.get_nifty_index_historical_1m(
                    trade_date=td, persist=True
                )
                downloaded = True
        except Exception as e:
            logger.warning("niftychart hist: index download failed for %s: %s", trade_date, e)

    if not nifty_bars:
        return {
            "status": "no_data",
            "message": (
                f"No Nifty 1m data for {trade_date} in live DB or strangle cache. "
                f"Load the day in /backtest-ui first (HISTORICAL_BROKER={hist_name}), "
                "or run the live algo that session. NIFTY_PREOPEN_PRICE is only for live strike selection."
            ),
            "history": [],
            "count": 0,
        }

    spot_open = float(nifty_bars[0].open)
    preopen = getattr(config, "NIFTY_PREOPEN_PRICE", None)
    strike_spot = float(preopen) if preopen and float(preopen) > 0 else spot_open
    atm = int(round(strike_spot / interval) * interval)
    want_ce, want_pe = atm + offset, atm - offset

    preferred_exp = _expiry_for_historical_trade_date(td)
    available = _strangle_legs_available_for_trade_date(td)
    expiry_date = preferred_exp
    ce_strike, pe_strike = want_ce, want_pe

    if available:
        # Prefer env expiry match, else first available pair; prefer ATM±offset strikes when present
        match = next((a for a in available if a[0] == preferred_exp), None)
        if match is None:
            match = available[0]
        expiry_date, ce_db, pe_db = match
        # If multiple strikes for same expiry, prefer computed ATM±offset
        same_exp = [a for a in available if a[0] == expiry_date]
        preferred_pair = next(
            (a for a in same_exp if a[1] == want_ce and a[2] == want_pe),
            None,
        )
        if preferred_pair:
            ce_strike, pe_strike = preferred_pair[1], preferred_pair[2]
        else:
            ce_strike, pe_strike = ce_db, pe_db

    ce_h, pe_h = get_strangle_history_from_db(
        expiry_date=expiry_date,
        trade_date=td,
        ce_strike=ce_strike,
        pe_strike=pe_strike,
    )

    if not (ce_h and pe_h):
        try:
            client = get_historical_client(config)
            if client.connect():
                ce_h, pe_h = client.get_strangle_historical_1m(
                    expiry_date=expiry_date,
                    ce_strike=ce_strike,
                    pe_strike=pe_strike,
                    trade_date=td,
                )
                downloaded = True
        except Exception as e:
            logger.warning("niftychart hist: strangle download failed for %s: %s", trade_date, e)

    if not (ce_h and pe_h):
        return {
            "status": "no_data",
            "message": (
                f"No CE/PE 1m for {trade_date} expiry={expiry_date} CE={ce_strike} PE={pe_strike}. "
                f"Open /backtest-ui and load that day (or set NIFTY_EXPIRY_DATE to the contract you cached)."
            ),
            "history": [],
            "count": 0,
        }

    ce_by_ts = {c.datetime.strftime("%H:%M:%S"): c for c in ce_h}
    pe_by_ts = {p.datetime.strftime("%H:%M:%S"): p for p in pe_h}

    tracker = NiftyChartTracker(
        atm_offset_points=offset,
        strike_interval=interval,
        lot_size=int(getattr(config, "ORDER_QTY", 75)),
    )
    tracker.reset()
    # Lock offset strikes to the strangle we actually loaded (not re-derived mid-session)
    tracker._offset_ce = ce_strike
    tracker._offset_pe = pe_strike

    history: list[dict[str, Any]] = []
    or_high = or_low = None
    or_frozen = False

    for bar in nifty_bars:
        tdt = bar.datetime
        ts_key = tdt.strftime("%H:%M:%S")
        ce = ce_by_ts.get(ts_key)
        pe = pe_by_ts.get(ts_key)
        if ce is None or pe is None:
            continue
        spot = float(bar.close)
        tt = tdt.time().replace(microsecond=0)
        if not or_frozen and OR_START <= tt < OR_END:
            or_high = spot if or_high is None else max(or_high, spot)
            or_low = spot if or_low is None else min(or_low, spot)
        if tt >= OR_END:
            or_frozen = True

        chain = [
            {
                "strike": ce_strike,
                "option_type": "CE",
                "ltp": ce.close,
                "LTP": ce.close,
                "volume": ce.volume,
                "Volume": ce.volume,
            },
            {
                "strike": pe_strike,
                "option_type": "PE",
                "ltp": pe.close,
                "LTP": pe.close,
                "volume": pe.volume,
                "Volume": pe.volume,
            },
        ]
        snap = tracker.update(chain, spot, record_history=True, now=tdt)
        if snap:
            row = tracker.snapshot_to_dict(snap)
            row["timestamp"] = tdt.isoformat(sep="T", timespec="seconds")
            row["or_high"] = or_high if or_high and or_high > 0 else None
            row["or_low"] = or_low if or_low and or_low > 0 else None
            history.append(row)

    if limit > 0 and len(history) > limit:
        history = history[:limit]

    if not history:
        return {
            "status": "no_data",
            "message": f"Could not align Nifty/CE/PE timestamps for {trade_date}",
            "history": [],
            "count": 0,
        }

    return {
        "status": "ok",
        "current": history[-1],
        "history": history,
        "count": len(history),
        "volume_chart": _volume_chart_display_for_live(),
        "or_levels": {"or_high": or_high, "or_low": or_low},
        "data_source": "strangle_cache_download" if downloaded else "strangle_cache",
        "strikes": {
            "expiry_date": expiry_date.isoformat(),
            "ce_strike": ce_strike,
            "pe_strike": pe_strike,
            "atm": atm,
        },
        "message": (
            "Loaded from backtest strangle cache (offset strangle + Nifty). "
            "Full volume-range chain needs live-algo snapshots."
        ),
    }


@app.get("/niftychart/history/by-date")
def get_niftychart_history_by_date(trade_date: str, limit: int = 0):
    try:
        date.fromisoformat(trade_date)
    except ValueError:
        return {"status": "error", "message": "trade_date must be YYYY-MM-DD", "history": [], "count": 0}
    lim = max(0, int(limit))
    live = _build_nifty_historical_history_from_db(trade_date, lim)
    if live.get("status") == "ok" and live.get("history"):
        live["data_source"] = "live_snapshots"
        return live
    # Fall back to backtest cache / HISTORICAL_BROKER (same data as /backtest-ui)
    cached = _build_nifty_historical_history_from_strangle_db(trade_date, lim)
    if cached.get("status") == "ok" and cached.get("history"):
        return cached
    # Prefer the more specific message from strangle fallback when live only said "No spot"
    if cached.get("message"):
        return cached
    return live


@app.get("/niftychart/current")
def get_niftychart_current():
    from nifty_algo.core.nifty_chart_tracker import get_nifty_chart_tracker

    cache = _get_active_cache()
    spot, _ = cache.get_spot()
    options = cache.get_latest_options(limit=500)
    tracker = get_nifty_chart_tracker()
    if spot is not None and spot > 0:
        tracker.update(options, spot, record_history=False)
    current = tracker.get_current()
    if current is None:
        return {"status": "no_data", "message": "No nifty chart data yet"}
    return {"status": "ok", "current": current, "or_levels": tracker.get_or_levels()}


#-------------------------
# Algo control (start from /niftychart UI)
#-------------------------

@app.get("/algo/status")
def algo_status():
    from nifty_algo.api.algo_runner import get_algo_status

    st = get_algo_status()
    # When main.py embeds API server, algo is already running in-process
    if hasattr(app.state, "cache") and app.state.cache is not None:
        st["embedded"] = True
        st["running"] = True
    return st


@app.post("/algo/start")
def algo_start(dry_run: bool | None = None):
    from nifty_algo.api.algo_runner import start_algo

    if hasattr(app.state, "cache") and app.state.cache is not None:
        return {"status": "embedded", "message": "Algo already running in this process (ENABLE_API_SERVER from main)."}
    return start_algo(dry_run=dry_run)


@app.post("/algo/stop")
def algo_stop():
    from nifty_algo.api.algo_runner import stop_algo

    if hasattr(app.state, "cache") and app.state.cache is not None:
        return {"status": "embedded", "message": "Cannot stop embedded algo from API; stop the main process."}
    return stop_algo()


@app.get("/algo/logs")
def algo_logs(tail: int = 40):
    from nifty_algo.api.algo_runner import get_algo_logs

    return {"logs": get_algo_logs(tail)}


def _get_chart_html_path() -> Path:
    """Path to chart page HTML (next to this module)."""
    return Path(__file__).resolve().parent / "templates" / "chart.html"


@app.get("/chart")
def get_chart_page():
    """Serve the strangle chart page from templates/chart.html."""
    from fastapi.responses import HTMLResponse

    path = _get_chart_html_path()
    if not path.exists():
        logger.error("Chart template not found: %s", path)
        return HTMLResponse(content="<h1>Chart template not found</h1>", status_code=500)
    html_content = path.read_text(encoding="utf-8")
    return HTMLResponse(content=html_content)


def _get_niftychart_html_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / "niftychart.html"


@app.get("/config/expiry")
def config_expiry():
    """Shared expiry resolution for UIs — prefer ``NIFTY_EXPIRY_DATE``; Fyers style is auto from that date."""
    from nifty_algo.market.expiry import expiry_resolution_info

    return {"status": "ok", **expiry_resolution_info(None)}


@app.get("/niftychart")
def get_niftychart_page():
    """Nifty chart with volume range, OTM range, opening range, dual volume panels."""
    from fastapi.responses import HTMLResponse

    path = _get_niftychart_html_path()
    if not path.exists():
        return HTMLResponse(content="<h1>niftychart.html not found</h1>", status_code=500)
    return HTMLResponse(content=path.read_text(encoding="utf-8"))

#-------------------------
# WebSocket Real-time Push
#-------------------------

# Global state for async event loop bridging
_event_loop: asyncio.AbstractEventLoop | None = None
_update_queue: asyncio.Queue | None = None


class ConnectionManager:
    """Manage WebSocket connections and real-time updates.
    Uses list (not set): WebSocket instances are not hashable.
    """

    active_connections: list[WebSocket]

    def __init__(self) -> None:
        self.active_connections = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("WebSocket connected. Active: %d", len(self.active_connections))
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info("WebSocket disconnected. Active: %d", len(self.active_connections))

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        if not self.active_connections:
            return
            
        disconnected = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                disconnected.append(conn)
        
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()


def _strangle_snapshot_for_ws() -> tuple[Any, Any]:
    """
    Latest strangle for WS push: prefer last history row; if history is empty but
    get_current() has a premium, build a point so the chart can move on updates.
    """
    from nifty_algo.core.strangle_tracker import get_strangle_tracker

    tracker = get_strangle_tracker()
    cur = tracker.get_current()
    history = tracker.get_history(-1)
    if history:
        return cur, history[-1]
    if cur and cur.get("total_premium") is not None:
        ts = datetime.now().isoformat()
        ce_v = float(cur.get("ce_volume") or 0.0)
        pe_v = float(cur.get("pe_volume") or 0.0)
        snap = {
            "timestamp": ts,
            "ce_strike": cur.get("ce_strike"),
            "pe_strike": cur.get("pe_strike"),
            "ce_price": cur.get("ce_price"),
            "pe_price": cur.get("pe_price"),
            "ce_volume": ce_v,
            "pe_volume": pe_v,
            "volume_diff": ce_v - pe_v,
            "total_premium": cur.get("total_premium"),
            "spot_price": cur.get("spot_price"),
        }
        return cur, snap
    return cur, None


def _enqueue_update(data: list[dict], spot: float):
    """
    Thread-safe callback for cache updates.
    Called from main thread, safely enqueues to async queue.
    """
    global _event_loop, _update_queue

    if _event_loop is None or _update_queue is None:
        logger.debug("Skip cache broadcast: event loop not ready yet")
        return

    # Clean data for JSON serialization (remove datetime objects)
    clean_data = [{k: v for k, v in opt.items() if k != "_cache_ts"} for opt in data[-20:]]
    
    # Strangle data: tracker was already updated in cache.publish_options() (before
    # subscribers are called), so get_current() / get_history() reflect this update.
    strangle_current = None
    strangle_snapshot = None
    nifty_current = None
    nifty_snapshot = None
    try:
        strangle_current, strangle_snapshot = _strangle_snapshot_for_ws()
    except Exception as e:
        logger.debug("Strangle snapshot for WS: %s", e)
    try:
        nifty_current, nifty_snapshot = _nifty_chart_snapshot_for_ws()
    except Exception as e:
        logger.debug("Nifty chart snapshot for WS: %s", e)
    
    message = {
        "type": "update",
        "spot_price": spot,
        "timestamp": datetime.now().isoformat(),
        "options": clean_data,
        "strangle": strangle_current,
        "strangle_snapshot": strangle_snapshot,
        "nifty_chart": nifty_current,
        "nifty_chart_snapshot": nifty_snapshot,
    }
    
    try:
        # Thread-safe way to put into asyncio queue from sync code
        _event_loop.call_soon_threadsafe(_update_queue.put_nowait, message)
    except Exception as e:
        logger.debug("Failed to enqueue update: %s", e)


async def _broadcast_worker():
    """
    Async worker that awaits on queue and broadcasts to WebSocket clients.
    Efficient - only wakes up when there's actually a message.
    """
    global _update_queue
    
    logger.info("Broadcast worker started")
    
    while True:
        try:
            # Efficiently wait for next message (no polling!)
            message = await _update_queue.get()
            
            if manager.active_connections:
                await manager.broadcast(message)
                logger.debug("Broadcast update: spot=%.2f, clients=%d", 
                           message.get("spot_price", 0), 
                           len(manager.active_connections))
        except asyncio.CancelledError:
            logger.info("Broadcast worker stopped")
            break
        except Exception as e:
            logger.error("Broadcast worker error: %s", e)


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time updates.
    
    - Sends initial snapshot on connect (including strangle history)
    - Receives push updates automatically via broadcast worker
    - Supports 'refresh' command from client
    """
    await manager.connect(websocket)

    # Send initial snapshot
    cache = _get_active_cache()
    spot, ts = cache.get_spot()
    
    # Clean options data for JSON serialization (remove datetime objects)
    options = cache.get_latest_options(limit=200)
    clean_options = []
    for opt in options:
        clean_opt = {k: v for k, v in opt.items() if k != "_cache_ts"}
        clean_options.append(clean_opt)
    
    # Seed strangle tracker from cache so chart has data even if main loop hasn't published recently
    strangle_current = None
    strangle_history = []
    try:
        from nifty_algo.core.strangle_tracker import get_strangle_tracker
        tracker = get_strangle_tracker()
        if spot is not None and spot > 0:
            tracker.update(options, spot, record_history=False)
        strangle_current = tracker.get_current()
        strangle_history = tracker.get_history(0)
    except Exception as e:
        logger.debug("Strangle tracker on connect: %s", e)
    
    await websocket.send_json({
        "type": "initial",
        "spot_price": spot,
        "spot_timestamp": ts.isoformat() if ts else None,
        "options": clean_options,
        "strangle": strangle_current,
        "strangle_history": strangle_history,
        "volume_chart": _volume_chart_display_for_live(),
    })

    try:
        while True:
            # Wait for client messages (refresh requests, etc.)
            data = await websocket.receive_text()
            
            if data == "refresh":
                spot, ts = cache.get_spot()
                options = cache.get_latest_options(limit=200)
                clean_options = [{k: v for k, v in opt.items() if k != "_cache_ts"} for opt in options]
                
                try:
                    tracker = get_strangle_tracker()
                    if spot is not None and spot > 0:
                        tracker.update(options, spot, record_history=False)
                    strangle_current = tracker.get_current()
                    strangle_history = tracker.get_history(0)
                except Exception:
                    strangle_current = None
                    strangle_history = []
                
                await websocket.send_json({
                    "type": "refresh",
                    "spot_price": spot,
                    "spot_timestamp": ts.isoformat() if ts else None,
                    "options": clean_options,
                    "strangle": strangle_current,
                    "strangle_history": strangle_history,
                    "volume_chart": _volume_chart_display_for_live(),
                })
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.debug("WebSocket closed: %s", e)
        manager.disconnect(websocket)


@app.websocket("/ws/nifty")
async def websocket_nifty_endpoint(websocket: WebSocket):
    """WebSocket for /niftychart — pushes nifty chart history + live snapshots."""
    await manager.connect(websocket)
    cache = _get_active_cache()
    spot, ts = cache.get_spot()
    options = cache.get_latest_options(limit=500)
    clean_options = [{k: v for k, v in opt.items() if k != "_cache_ts"} for opt in options]

    nifty_current = None
    nifty_history = []
    or_levels = {"or_high": None, "or_low": None}
    try:
        from nifty_algo.core.nifty_chart_tracker import get_nifty_chart_tracker
        tracker = get_nifty_chart_tracker()
        _ensure_live_nifty_session(tracker)
        if spot is not None and spot > 0:
            tracker.update(options, spot, record_history=False)
        nifty_current = tracker.get_current()
        nifty_history = tracker.get_history(0)
        or_levels = tracker.get_or_levels()
    except Exception as e:
        logger.debug("Nifty tracker on connect: %s", e)

    await websocket.send_json({
        "type": "initial",
        "spot_price": spot,
        "spot_timestamp": ts.isoformat() if ts else None,
        "nifty_chart": nifty_current,
        "nifty_chart_history": nifty_history,
        "or_levels": or_levels,
        "volume_chart": _volume_chart_display_for_live(),
    })

    try:
        while True:
            data = await websocket.receive_text()
            if data == "refresh":
                spot, ts = cache.get_spot()
                options = cache.get_latest_options(limit=500)
                try:
                    from nifty_algo.core.nifty_chart_tracker import get_nifty_chart_tracker
                    tracker = get_nifty_chart_tracker()
                    if spot is not None and spot > 0:
                        tracker.update(options, spot, record_history=False)
                    nifty_current = tracker.get_current()
                    nifty_history = tracker.get_history(0)
                    or_levels = tracker.get_or_levels()
                except Exception:
                    nifty_current = None
                    nifty_history = []
                await websocket.send_json({
                    "type": "refresh",
                    "spot_price": spot,
                    "spot_timestamp": ts.isoformat() if ts else None,
                    "nifty_chart": nifty_current,
                    "nifty_chart_history": nifty_history,
                    "or_levels": or_levels,
                    "volume_chart": _volume_chart_display_for_live(),
                })
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.debug("Nifty WS closed: %s", e)
        manager.disconnect(websocket)


#-------------------------
# Run standalone
#-------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
