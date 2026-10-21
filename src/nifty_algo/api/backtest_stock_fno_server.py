from __future__ import annotations

import logging
import sqlite3
import csv
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, model_validator

from nifty_algo.config import config

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").is_file():
            return p
    return Path.cwd()


def _db_path() -> Path:
    p = _project_root() / "db" / "stock_fno.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_fno_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            exchange_code TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            source TEXT NOT NULL DEFAULT 'breeze',
            created_at TEXT NOT NULL,
            UNIQUE(symbol, exchange_code, expiry_date, timestamp)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_fno_key "
        "ON stock_fno_history(symbol, exchange_code, expiry_date, timestamp)"
    )
    conn.commit()
    return conn


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def _range_to_breeze_iso(start_date: date, end_date: date) -> tuple[str, str]:
    """
    Breeze historical API behaves best with session-clock timestamps in IST formatted
    as ISO-with-Z (same convention used in existing breeze.py in this repo).
    """
    start_dt = datetime.combine(start_date, time(9, 15, 0))
    end_dt = datetime.combine(end_date, time(15, 30, 0))
    return (
        start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )


def _expiry_to_breeze(expiry: date) -> str:
    return f"{expiry.isoformat()}T07:00:00.000Z"


def _stock_code_candidates(symbol: str) -> list[str]:
    """
    Breeze derivatives often use compact stock codes (e.g. AXIBAN, TATMOT).
    Try a few deterministic candidates before giving up.
    """
    s = (symbol or "").strip().upper()
    out: list[str] = []
    for c in (
        s,
        s[:6] if len(s) > 6 else s,
        (s[:3] + "BAN") if s.endswith("BANK") and len(s) >= 3 else None,
        (s[:3] + "MOT") if s.endswith("MOTORS") and len(s) >= 3 else None,
        (s[:3] + "ENT") if s.endswith("ENT") or s.endswith("ENTS") or s.endswith("ENTERPRISES") else None,
    ):
        if c and c not in out:
            out.append(c)
    return out


def _augment_candidates_with_master(symbol: str, master_matches: list[dict[str, str]]) -> list[str]:
    out = _stock_code_candidates(symbol)
    for m in master_matches:
        c = (m.get("code") or "").strip().upper()
        if c and c not in out:
            out.append(c)
    return out


def _base_symbol_from_code(code: str) -> str:
    """
    Normalize script code to user-facing symbol.
    Example: FUT-PERSYS-30-MAR-2026 -> PERSYS
    """
    c = (code or "").strip().upper()
    if c.startswith("FUT-"):
        parts = c.split("-")
        if len(parts) >= 2 and parts[1]:
            return parts[1].strip().upper()
    return c


@dataclass
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_json(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class HistoricalInput(BaseModel):
    symbol: str
    expiry_date: date
    start_date: date
    end_date: date | None = None
    exchange_code: str = "NFO"

    @model_validator(mode="after")
    def _validate(self) -> "HistoricalInput":
        self.symbol = self.symbol.strip().upper()
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.end_date is None:
            self.end_date = self.expiry_date
        if self.end_date < self.start_date:
            raise ValueError("end_date must be >= start_date")
        return self


class BreezeStockFnoClient:
    def __init__(self, api_key: str, api_secret: str, session_token: str | None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_token = session_token or ""
        self._breeze = None
        self._connect_error: str | None = None
        self._nfo_symbol_master: list[dict[str, str]] = []

    def connect(self) -> bool:
        try:
            from breeze_connect import BreezeConnect
        except Exception:
            self._connect_error = "breeze-connect not installed"
            return False
        if not self.api_key or not self.api_secret or not self.session_token:
            self._connect_error = "Missing Breeze credentials/session token"
            return False
        try:
            b = BreezeConnect(api_key=self.api_key)
            b.generate_session(api_secret=self.api_secret, session_token=self.session_token)
            self._breeze = b
            self._load_symbol_master()
            return True
        except Exception as exc:
            self._connect_error = str(exc)
            self._breeze = None
            return False

    def _load_symbol_master(self) -> None:
        try:
            import requests
            from breeze_connect import config as bc_config

            with requests.Session() as s:
                resp = s.get(bc_config.STOCK_SCRIPT_CSV_URL, timeout=20)
                resp.raise_for_status()
            rows = list(csv.reader(resp.text.splitlines(), delimiter=","))
            out: list[dict[str, str]] = []
            for r in rows:
                if len(r) < 8 or r[2] != "NFO":
                    continue
                code = (r[7] or "").strip().upper()
                name = (r[1] or "").strip().upper()
                if code:
                    out.append(
                        {
                            "code": code,
                            "base_symbol": _base_symbol_from_code(code),
                            "name": name,
                            "segment": "NFO",
                        }
                    )
            self._nfo_symbol_master = out
            logger.info("Loaded Breeze NFO symbol master rows: %s", len(out))
        except Exception as exc:
            logger.warning("Failed to load Breeze symbol master: %s", exc)
            self._nfo_symbol_master = []

    def search_nfo_symbols(self, q: str, limit: int = 30) -> list[dict[str, str]]:
        t = (q or "").strip().upper()
        if not t:
            return self._nfo_symbol_master[:limit]
        pref = [
            x
            for x in self._nfo_symbol_master
            if x["base_symbol"].startswith(t) or x["code"].startswith(t)
        ]
        name_pref = [x for x in self._nfo_symbol_master if x["name"].startswith(t)]
        contains = [
            x
            for x in self._nfo_symbol_master
            if t in x["base_symbol"] or t in x["code"] or t in x["name"]
        ]
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for grp in (pref, name_pref, contains):
            for it in grp:
                k = it["code"]
                if k in seen:
                    continue
                seen.add(k)
                out.append(it)
                if len(out) >= limit:
                    return out
        return out[:limit]

    def require(self) -> None:
        if self._breeze is None:
            raise RuntimeError(self._connect_error or "Breeze not connected")

    def fetch_futures_30m(
        self,
        *,
        symbol: str,
        exchange_code: str,
        expiry_date: date,
        start_date: date,
        end_date: date,
        master_candidates: list[dict[str, str]] | None = None,
    ) -> tuple[list[Candle], str, date, str]:
        self.require()
        from_iso, to_iso = _range_to_breeze_iso(start_date, end_date)
        stock_candidates = _augment_candidates_with_master(symbol, master_candidates or [])
        expiry_candidates = [
            expiry_date,
            expiry_date + timedelta(days=1),
            expiry_date + timedelta(days=2),
            expiry_date + timedelta(days=3),
            expiry_date - timedelta(days=1),
            expiry_date - timedelta(days=2),
        ]
        attempts = [
            {
                "exchange_code": exchange_code,
                "product_type": "futures",
                "right": "others",
                "strike_price": "0",
            },
            {
                "exchange_code": exchange_code,
                "product_type": "futures",
            },
            {
                "exchange_code": "NFO",
                "product_type": "futures",
                "right": "others",
                "strike_price": "0",
            },
        ]
        last_reason = "no_data"

        for stock_i, stock_code in enumerate(stock_candidates, start=1):
            for exp_i, exp_date in enumerate(expiry_candidates, start=1):
                expiry_str = _expiry_to_breeze(exp_date)
                for i, extra in enumerate(attempts, start=1):
                    payload = {
                        "interval": "30minute",
                        "from_date": from_iso,
                        "to_date": to_iso,
                        "stock_code": stock_code,
                        "expiry_date": expiry_str,
                        **extra,
                    }
                    try:
                        resp = self._breeze.get_historical_data_v2(**payload)
                    except Exception as exc:
                        last_reason = f"exception: {exc}"
                        logger.warning(
                            "Breeze futures fetch stock_probe=%s expiry_probe=%s attempt=%s failed: %s",
                            stock_i,
                            exp_i,
                            i,
                            exc,
                        )
                        continue

                    if resp.get("Error"):
                        last_reason = str(resp.get("Error"))
                        logger.warning(
                            "Breeze futures error stock_probe=%s expiry_probe=%s attempt=%s: %s",
                            stock_i,
                            exp_i,
                            i,
                            last_reason,
                        )
                        continue

                    rows = resp.get("Success") or []
                    out: list[Candle] = []
                    for r in rows:
                        ts = str(r.get("datetime", "")).strip()
                        if not ts:
                            continue
                        try:
                            out.append(
                                Candle(
                                    timestamp=ts,
                                    open=float(r.get("open", 0) or 0),
                                    high=float(r.get("high", 0) or 0),
                                    low=float(r.get("low", 0) or 0),
                                    close=float(r.get("close", 0) or 0),
                                    volume=float(r.get("volume", 0) or 0),
                                )
                            )
                        except (TypeError, ValueError):
                            continue
                    if out:
                        return (
                            out,
                            f"breeze_stock_probe={stock_i}_expiry_probe={exp_i}_attempt={i}",
                            exp_date,
                            stock_code,
                        )
                    last_reason = (
                        f"empty_success stock_probe={stock_i} expiry_probe={exp_i} attempt={i}"
                    )
                    logger.warning(
                        "Breeze futures returned empty Success on stock_probe=%s expiry_probe=%s attempt=%s | payload=%s",
                        stock_i,
                        exp_i,
                        i,
                        payload,
                    )

        return [], last_reason, expiry_date, symbol


def _load_from_db(
    *,
    symbol: str,
    exchange_code: str,
    expiry_date: date,
    start_date: date,
    end_date: date,
) -> list[Candle]:
    conn = _connect_db()
    try:
        rows = conn.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM stock_fno_history
            WHERE symbol = ? AND exchange_code = ? AND expiry_date = ?
              AND substr(timestamp, 1, 10) >= ? AND substr(timestamp, 1, 10) <= ?
            ORDER BY timestamp ASC
            """,
            (
                symbol,
                exchange_code,
                expiry_date.isoformat(),
                start_date.isoformat(),
                end_date.isoformat(),
            ),
        ).fetchall()
        return [
            Candle(
                timestamp=r["timestamp"],
                open=float(r["open"] or 0),
                high=float(r["high"] or 0),
                low=float(r["low"] or 0),
                close=float(r["close"] or 0),
                volume=float(r["volume"] or 0),
            )
            for r in rows
        ]
    finally:
        conn.close()


def _save_to_db(symbol: str, exchange_code: str, expiry_date: date, candles: list[Candle]) -> int:
    if not candles:
        return 0
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _connect_db()
    try:
        cur = conn.cursor()
        for c in candles:
            cur.execute(
                """
                INSERT OR REPLACE INTO stock_fno_history (
                    symbol, exchange_code, expiry_date, timestamp, open, high, low, close, volume, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'breeze', ?)
                """,
                (
                    symbol,
                    exchange_code,
                    expiry_date.isoformat(),
                    c.timestamp,
                    c.open,
                    c.high,
                    c.low,
                    c.close,
                    c.volume,
                    now,
                ),
            )
        conn.commit()
        return len(candles)
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = BreezeStockFnoClient(
        api_key=config.BREEZE_API_KEY,
        api_secret=config.BREEZE_API_SECRET,
        session_token=config.BREEZE_SESSION_TOKEN or None,
    )
    app.state.breeze_client = client
    app.state.breeze_ok = client.connect()
    if not app.state.breeze_ok:
        logger.warning("Breeze unavailable for stock FNO API: %s", client._connect_error)
    yield


app = FastAPI(title="Stock FNO Backtest Service", version="1.0.0", lifespan=lifespan)


@app.get("/backtest-stock-fno-ui")
def ui():
    path = Path(__file__).resolve().parent / "backtest_stock_fno_ui.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.post("/backtest/stock-fno/historical")
def historical(req: HistoricalInput):
    end_date = req.end_date or req.expiry_date
    cached = _load_from_db(
        symbol=req.symbol,
        exchange_code=req.exchange_code,
        expiry_date=req.expiry_date,
        start_date=req.start_date,
        end_date=end_date,
    )
    source = "database"
    candles = cached

    if not candles:
        client: BreezeStockFnoClient = app.state.breeze_client
        master_matches = client.search_nfo_symbols(req.symbol, limit=20)
        try:
            candles, fetch_reason, resolved_expiry, resolved_stock_code = client.fetch_futures_30m(
                symbol=req.symbol,
                exchange_code=req.exchange_code,
                expiry_date=req.expiry_date,
                start_date=req.start_date,
                end_date=end_date,
                master_candidates=master_matches,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not candles:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No data from Breeze for this symbol/expiry/date range. "
                    f"Reason: {fetch_reason}"
                ),
            )
        _save_to_db(req.symbol, req.exchange_code, req.expiry_date, candles)
        source = "breeze"
    else:
        resolved_expiry = req.expiry_date
        resolved_stock_code = req.symbol

    return {
        "status": "ok",
        "source": source,
        "db_path": str(_db_path()),
        "inputs": {
            "symbol": req.symbol,
            "exchange_code": req.exchange_code,
            "expiry_date": req.expiry_date.isoformat(),
            "resolved_expiry_date": resolved_expiry.isoformat(),
            "resolved_stock_code": resolved_stock_code,
            "start_date": req.start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "count": len(candles),
        "candles": [c.to_json() for c in candles],
    }


@app.get("/backtest/stock-fno/symbols")
def symbol_list(
    q: str = Query("", description="Search text"),
    limit: int = Query(30, ge=1, le=200),
):
    client: BreezeStockFnoClient = app.state.breeze_client
    rows = client.search_nfo_symbols(q, limit=limit)
    return {
        "status": "ok",
        "count": len(rows),
        "symbols": rows,
    }

