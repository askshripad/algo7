"""
ICICI Breeze API client for historical options data.
Uses breeze-connect SDK: pip install breeze-connect

Session: Set BREEZE_SESSION_TOKEN from browser after visiting
  https://api.icicidirect.com/apiuser/login?api_key=YOUR_API_KEY
Then generate_session(api_secret=..., session_token=...) is called once to link the SDK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from pathlib import Path
from nifty_algo.data.strangle_history_db import save_strangle_history
from nifty_algo.data.strangle_history_db import get_strangle_history_from_db

try:
    from breeze_connect import BreezeConnect

    BREEZE_AVAILABLE = True
except Exception:
    BreezeConnect = None
    BREEZE_AVAILABLE = False

logger = logging.getLogger(__name__)

# NSE market hours 9:15–15:30 IST; convert to UTC for Breeze API.
IST_OFFSET_HOURS = 5
IST_OFFSET_MINUTES = 30


@dataclass
class OptionCandle:
    """One 1-minute OHLCV bar (normalized from Breeze response)."""

    datetime: datetime  # bar start time (naive, as returned by Breeze)
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "datetime": (
                self.datetime.isoformat()
                if self.datetime.tzinfo
                else self.datetime.strftime("%Y-%m-%d %H:%M:%S")
            ),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def _ist_to_utc_iso(dt_ist: datetime) -> str:
    """Convert naive datetime in IST to Breeze-style UTC ISO string."""
    utc = dt_ist - timedelta(hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINUTES)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _trade_date_to_from_to_utc(trade_date: date) -> tuple[str, str]:
    """Full NSE session on trade_date (IST) as Breeze from_date / to_date (UTC)."""
    from_dt = datetime.combine(trade_date, time(9, 15, 0))
    to_dt = datetime.combine(trade_date, time(15, 30, 0))
    return (
        from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        to_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )
    #return _ist_to_utc_iso(from_dt), _ist_to_utc_iso(to_dt)


def _breeze_expiry_string(expiry_date: date) -> str:
    """Breeze expects e.g. 2025-02-06T07:00:00.000Z."""
    return f"{expiry_date.isoformat()}T07:00:00.000Z"


def _parse_breeze_datetime(dt_str: str) -> datetime:
    """Parse Breeze 'datetime' field (e.g. '2025-02-03 09:20:00')."""
    try:
        return datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime(2000, 1, 1)


class BreezeHistoricalClient:
    """
    Breeze client for historical options data only (no live trading).
    Fetch 1-minute CE/PE OHLCV for a strike and expiry.
    """

    source_name: Literal["breeze"] = "breeze"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        session_token: str | None = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_token = session_token or ""
        self._breeze: Any = None
        self._connect_error: str | None = None

    def connect(self) -> bool:
        """
        Initialize Breeze SDK and session.
        Requires BREEZE_SESSION_TOKEN from the browser login URL.

        Returns False on missing config, expired session, or SDK errors — does not raise
        (callers can start the app and serve DB-backed routes only).
        """
        self._connect_error = None
        if not BREEZE_AVAILABLE:
            self._connect_error = (
                "breeze-connect not installed (pip install breeze-connect)"
            )
            logger.warning("Breeze: %s", self._connect_error)
            return False
        if not self.api_key or not self.api_secret:
            self._connect_error = "BREEZE_API_KEY and BREEZE_API_SECRET must be set"
            logger.warning("Breeze: %s", self._connect_error)
            return False
        if not self.session_token:
            self._connect_error = (
                "BREEZE_SESSION_TOKEN missing. Visit "
                "https://api.icicidirect.com/apiuser/login?api_key=YOUR_API_KEY"
            )
            logger.warning("Breeze: %s", self._connect_error)
            return False

        self._breeze = BreezeConnect(api_key=self.api_key)
        try:
            self._breeze.generate_session(
                api_secret=self.api_secret,
                session_token=self.session_token,
            )
        except Exception as e:
            self._connect_error = str(e).strip() or repr(e)
            logger.warning("Breeze generate_session failed: %s", self._connect_error)
            self._breeze = None
            return False

        logger.info("Breeze session created successfully")
        return True

    def _require_breeze(self) -> None:
        if self._breeze is None:
            msg = self._connect_error or (
                "Breeze not connected. Set BREEZE_SESSION_TOKEN from the ICICI login URL "
                "and restart (session tokens expire daily)."
            )
            raise RuntimeError(msg)

    def get_option_historical_1m(
        self,
        expiry_date: date,
        strike: int,
        option_type: str,
        trade_date: date,
        stock_code: str = "NIFTY",
        exchange_code: str = "NFO",
    ) -> list[OptionCandle]:
        """
        Fetch 1-minute OHLCV for one option (CE or PE).

        trade_date: calendar date in IST; uses session 9:15–15:30 IST → UTC for API.
        option_type: 'CE' or 'PE' (Breeze right call / put).
        """

        self._require_breeze()

        from_date_iso, to_date_iso = _trade_date_to_from_to_utc(trade_date)
        right = "call" if option_type.upper() == "CE" else "put"
        expiry_str = _breeze_expiry_string(expiry_date)

        try:
            resp = self._breeze.get_historical_data_v2(
                interval="1minute",
                from_date=from_date_iso,
                to_date=to_date_iso,
                stock_code=stock_code,
                exchange_code=exchange_code,
                product_type="options",
                expiry_date=expiry_str,
                strike_price=str(strike),
                right=right,
            )
        except Exception as e:
            logger.exception("Breeze get_historical_data_v2 failed: %s", e)
            return []

        if resp.get("Error"):
            logger.warning("Breeze API Error: %s", resp["Error"])
            return []

        rows = resp.get("Success") or []
        out: list[OptionCandle] = []
        for r in rows:
            try:
                dt = _parse_breeze_datetime(str(r.get("datetime", "")))
                out.append(
                    OptionCandle(
                        datetime=dt,
                        open=float(r.get("open", 0)),
                        high=float(r.get("high", 0)),
                        low=float(r.get("low", 0)),
                        close=float(r.get("close", 0)),
                        volume=float(r.get("volume", 0)),
                    )
                )
            except (TypeError, ValueError) as e:
                logger.debug("Skip row %s: %s", r, e)
        return out

    def get_strangle_historical_1m(
        self,
        expiry_date: date,
        ce_strike: int,
        pe_strike: int,
        trade_date: date,
        stock_code: str = "NIFTY",
        exchange_code: str = "NFO",
    ) -> tuple[list[OptionCandle], list[OptionCandle]]:
        """Fetch 1m history for CE and PE. Returns (ce_candles, pe_candles)."""
        ce_cached, pe_cached = get_strangle_history_from_db(
            expiry_date=expiry_date,
            trade_date=trade_date,
            ce_strike=ce_strike,
            pe_strike=pe_strike,
            symbol=stock_code,
            exchange_code=exchange_code,
        )
        if ce_cached and pe_cached:
            return ce_cached, pe_cached

        ce = self.get_option_historical_1m(
            expiry_date, ce_strike, "CE", trade_date, stock_code, exchange_code
        )
        pe = self.get_option_historical_1m(
            expiry_date, pe_strike, "PE", trade_date, stock_code, exchange_code
        )

        save_strangle_history(
            ce,
            pe,
            expiry_date=expiry_date,
            trade_date=trade_date,
            ce_strike=ce_strike,
            pe_strike=pe_strike,
            symbol=stock_code,
            exchange_code=exchange_code,
            source=self.source_name,
        )

        return ce, pe

    def get_nifty_index_historical_1m(
        self,
        trade_date: date,
        stock_code: str = "NIFTY",
        exchange_code: str = "NSE",
        *,
        persist: bool = False,
        db_path: Path | None = None,
    ) -> tuple[list[OptionCandle], str]:
        """
        1-minute NIFTY index (cash) OHLCV for trade_date.

        **Cache-first:** reads `nifty_index_history` in db/strangle_data.db when present;
        otherwise calls Breeze. When ``persist=True`` (recommended for backtests), a successful
        API response is written to the DB for next time.
        """
        from nifty_algo.data.strangle_history_db import get_nifty_index_history_from_db
        from nifty_algo.data.strangle_history_db import get_strangle_db_path
        from nifty_algo.data.strangle_history_db import save_nifty_index_historical_1m

        cached = get_nifty_index_history_from_db(
            symbol=stock_code,
            exchange_code=exchange_code,
            trade_date=trade_date,
        )
        if cached:
            logger.info(
                "Nifty index: %s 1m bars from SQLite %s (nifty_index_history) for %s — skipping Breeze",
                len(cached),
                get_strangle_db_path(),
                trade_date,
            )
            return cached, "database"

        self._require_breeze()

        from_date_iso, to_date_iso = _trade_date_to_from_to_utc(trade_date)

        try:
            resp = self._breeze.get_historical_data_v2(
                interval="1minute",
                from_date=from_date_iso,
                to_date=to_date_iso,
                stock_code=stock_code,
                exchange_code=exchange_code,
                product_type="cash",
            )
        except Exception as e:
            logger.exception("Breeze get_historical_data_v2 (cash) failed: %s", e)
            return [], self.source_name

        if resp.get("Error"):
            logger.warning("Breeze API Error: %s", resp["Error"])
            return [], self.source_name

        rows = resp.get("Success") or []
        out: list[OptionCandle] = []
        for r in rows:
            dt = _parse_breeze_datetime(str(r.get("datetime", "")))
            out.append(
                OptionCandle(
                    datetime=dt,
                    open=float(r.get("open", 0)),
                    high=float(r.get("high", 0)),
                    low=float(r.get("low", 0)),
                    close=float(r.get("close", 0)),
                    volume=float(r.get("volume", 0)),
                )
            )

        if out:
            logger.info(
                "Nifty index: fetched %s 1m bars from Breeze for %s",
                len(out),
                trade_date,
            )

        if out and persist:
            save_nifty_index_historical_1m(
                out,
                symbol=stock_code,
                exchange_code=exchange_code,
                trade_date=trade_date,
                db_path=db_path,
                source=self.source_name,
            )

        return out, self.source_name