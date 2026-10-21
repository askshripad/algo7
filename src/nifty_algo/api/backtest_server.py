from __future__ import annotations
import logging
from datetime import date
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator
from nifty_algo.brokers import get_historical_client
from nifty_algo.config import config
from nifty_algo.data.strangle_history_db import get_nifty_daily_row_before
from nifty_algo.data.strangle_history_db import get_nifty_daily_row_for_trade_date
from nifty_algo.data.strangle_history_db import get_nifty_spot_open_for_trade_date
from nifty_algo.data.strangle_history_db import get_strangle_db_path
from nifty_algo.data.strangle_history_db import get_strangle_history_from_db
from fastapi.responses import HTMLResponse
from pathlib import Path

logger = logging.getLogger(__name__)

def round_to_interval(x: float, interval: int) -> int:
    """Round x to nearest strike interval."""
    return int(round(x / interval) * interval)

class HistoricalInput(BaseModel):
    """
    Manual: pass ``ce_strike`` + ``pe_strike``.

    Auto: pass ``nifty_open`` = **spot (today's cash open)**. Strikes are
    ``CE = ATM + offset``, ``PE = ATM − offset`` where ``ATM`` is ``nifty_open`` rounded to the
    nearest ``STRIKE_INTERVAL`` (50). ``atr`` is optional (e.g. for reference only).
    """

    trade_date: date
    expiry_date: date
    nifty_open: float
    atr: float | None = None
    ce_strike: int | None = None
    pe_strike: int | None = None
    stock_code: str = "NIFTY"
    exchange_code: str = "NFO"

    @model_validator(mode="after")
    def atr_or_manual_strikes(self) -> "HistoricalInput":
        has_atr = self.atr is not None and self.atr > 0
        has_both = self.ce_strike is not None and self.pe_strike is not None
        has_partial = (self.ce_strike is not None) ^ (self.pe_strike is not None)
        if has_partial:
            raise ValueError("Provide both ce_strike and pe_strike, or neither (use ATM from nifty_open).")
        if has_atr and has_both:
            raise ValueError("Provide either manual strikes OR auto (nifty_open), not both ATR and CE/PE.")
        if has_both:
            if int(self.ce_strike) <= 0 or int(self.pe_strike) <= 0:
                raise ValueError("ce_strike and pe_strike must be positive integers.")
            return self
        if self.nifty_open is None or float(self.nifty_open) <= 0:
            raise ValueError("Positive nifty_open (spot / cash open) is required when CE/PE are not set.")
        return self

def _candle_to_json(candle: Any) -> dict[str, Any]:
    # breeze.py OptionCandle has `datetime, open, high, low, close, volume`
    dt = candle.datetime
    ts = dt.isoformat(sep=" ")
    return {
        "timestamp": ts,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }

def _premium_series(ce_history: list[Any], pe_history: list[Any]) -> list[dict[str, Any]]:
    # Align by timestamp string derived from candle.datetime
    ce_map = {}
    for c in ce_history:
        ce_map[c.datetime.isoformat(sep=" ")] = c
    series: list[dict[str, Any]] = []
    for p in pe_history:
        ts = p.datetime.isoformat(sep=" ")
        ce = ce_map.get(ts)
        if ce is None:
            continue
        series.append(
            {
                "timestamp": ts,
                "ce_close": ce.close,
                "pe_close": p.close,
                "total_premium": ce.close + p.close,
            }
        )
    return series

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Historical download client (HISTORICAL_BROKER=breeze|fyers). Session may be expired — app still starts.
    hist_name = (config.HISTORICAL_BROKER or "breeze").strip().lower()
    app.state.historical_broker = hist_name
    app.state.hist_client = get_historical_client(config)
    # Keep legacy alias used by older call sites
    app.state.breeze_client = app.state.hist_client
    if app.state.hist_client.connect():
        app.state.breeze_ok = True
        app.state.hist_ok = True
    else:
        app.state.breeze_ok = False
        app.state.hist_ok = False
        hint = (
            "set a fresh BREEZE_SESSION_TOKEN"
            if hist_name == "breeze"
            else "refresh FYERS_ACCESS_TOKEN via generate_token.py"
        )
        logger.warning(
            "Historical broker (%s) not available (%s). "
            "Endpoints that need live API fetch will return 503 until you %s and restart.",
            hist_name,
            getattr(app.state.hist_client, "_connect_error", None),
            hint,
        )
    yield
app = FastAPI(title="Backtest Historical Service", version="1.0.0", lifespan=lifespan)
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/backtest/nifty-daily-prev")
def nifty_daily_prev(trade_date: date = Query(..., description="Selected trade date (YYYY-MM-DD)")):
    """
    Return **Close** and **ATR** from ``nifty_daily`` for the **previous** row (latest date
    strictly before ``trade_date``). Not the trade-date row — see POST response
    ``nifty_daily_trade_date`` for that day's High/Low.
    """
    row = get_nifty_daily_row_before(trade_date)
    if not row:
        return {
            "status": "no_data",
            "message": "No nifty_daily row before this date. Add rows to db/strangle_data.db (table nifty_daily).",
        }
    if row.get("close") is None or row.get("atr") is None:
        return {"status": "no_data", "message": "nifty_daily row has missing close or atr."}
    return {
        "status": "ok",
        "ref_date": row["date"],
        "close": row["close"],
        "atr": row["atr"],
    }


@app.get("/backtest/nifty-spot-open")
def nifty_spot_open(trade_date: date = Query(..., description="Session date (YYYY-MM-DD)")):
    """
    **Today's cash open** for strike selection: ``nifty_daily.Open`` for ``trade_date``, else
    first 1m bar open in ``nifty_index_history``.
    """
    o, src = get_nifty_spot_open_for_trade_date(trade_date)
    if o is None:
        return {
            "status": "no_data",
            "message": "No Open in nifty_daily for this date and no index 1m row.",
        }
    return {"status": "ok", "open": o, "source": src}


@app.post("/backtest/historical/nifty-and-strangle")
def get_nifty_and_strangle_hist(req: HistoricalInput):
    # 1) Strikes: from ATR rule or explicit CE/PE
    if req.ce_strike is not None and req.pe_strike is not None:
        ce_strike = int(req.ce_strike)
        pe_strike = int(req.pe_strike)
        logger.info(
            "Backtest request (manual strikes): trade_date=%s expiry_date=%s nifty_open=%s CE=%s PE=%s",
            req.trade_date,
            req.expiry_date,
            req.nifty_open,
            ce_strike,
            pe_strike,
        )
    else:
        interval = config.STRIKE_INTERVAL
        off = config.STRANGLE_ATM_OFFSET_POINTS
        atm = round_to_interval(float(req.nifty_open), interval)
        ce_strike = atm + off
        pe_strike = atm - off
        logger.info(
            "Backtest request (ATM±%s): trade_date=%s expiry_date=%s spot=%s => ATM=%s CE=%s PE=%s",
            off,
            req.trade_date,
            req.expiry_date,
            req.nifty_open,
            atm,
            ce_strike,
            pe_strike,
        )
    client = app.state.hist_client
    hist_name = getattr(app.state, "historical_broker", None) or getattr(
        client, "source_name", "breeze"
    )

    # Pre-check strangle rows in SQLite (db/strangle_data.db) — metadata only.
    # Nifty source comes from get_nifty_index_historical_1m (same DB file, table nifty_index_history).
    ce_db, pe_db = get_strangle_history_from_db(
        expiry_date=req.expiry_date,
        trade_date=req.trade_date,
        ce_strike=ce_strike,
        pe_strike=pe_strike,
        symbol=req.stock_code,
        exchange_code=req.exchange_code,
    )
    data_sources = {
        "strangle_ce_pe": "database" if (ce_db and pe_db) else hist_name,
        "historical_broker": hist_name,
    }

    # 2) NIFTY cash 1m — SQLite first; configured HISTORICAL_BROKER only if missing.
    try:
        nifty_history, nifty_src = client.get_nifty_index_historical_1m(
            trade_date=req.trade_date,
            stock_code=req.stock_code,
            exchange_code="NSE",
            persist=True,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    data_sources["nifty_index"] = nifty_src
    # 3) Strangle CE/PE — DB first inside client; Breeze only if either leg missing
    try:
        ce_history, pe_history = client.get_strangle_historical_1m(
            expiry_date=req.expiry_date,
            ce_strike=ce_strike,
            pe_strike=pe_strike,
            trade_date=req.trade_date,
            stock_code=req.stock_code,
            exchange_code=req.exchange_code,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    # nifty_daily: High/Low range for the *selected trade date* (not the previous-day Close/ATR row)
    nifty_daily_trade_date = get_nifty_daily_row_for_trade_date(req.trade_date)

    # 4) Build response
    return {
        "status": "ok",
        "inputs": req.model_dump(),
        "cache_sqlite_path": str(get_strangle_db_path()),
        "data_sources": data_sources,
        "strikes": {
            "ce_strike": ce_strike,
            "pe_strike": pe_strike,
            **(
                {
                    "atm": round_to_interval(float(req.nifty_open), config.STRIKE_INTERVAL),
                    "atm_offset_points": config.STRANGLE_ATM_OFFSET_POINTS,
                }
                if req.ce_strike is None
                else {}
            ),
        },
        # High, low, range from nifty_daily row matching trade_date (see GET /backtest/nifty-daily-prev for prior close/ATR)
        "nifty_daily_trade_date": nifty_daily_trade_date,
        "nifty_history": [_candle_to_json(c) for c in nifty_history],
        "ce_history": [_candle_to_json(c) for c in ce_history],
        "pe_history": [_candle_to_json(c) for c in pe_history],
        "strangle_premium_1m": _premium_series(ce_history, pe_history),
    }

@app.get("/config/expiry")
def config_expiry():
    """Shared expiry resolution for UIs — prefer ``NIFTY_EXPIRY_DATE``; Fyers style is auto from that date."""
    from nifty_algo.market.expiry import expiry_resolution_info

    return {"status": "ok", **expiry_resolution_info(None)}


@app.get("/backtest-ui")
def backtest_ui():
    path = Path(__file__).resolve().parent / "backtest_ui.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


