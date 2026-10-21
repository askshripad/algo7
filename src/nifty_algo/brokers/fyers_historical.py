"""
Fyers historical 1-minute candles (Nifty index + option strangle).

Uses fyers_apiv3 ``FyersModel.history``. Same OptionCandle / SQLite cache shape as Breeze
so backtest can switch via ``HISTORICAL_BROKER=fyers``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from nifty_algo.brokers.breeze import OptionCandle
from nifty_algo.brokers.fyers_symbol_format import build_fyers_option_symbol_candidates
from nifty_algo.data.strangle_history_db import get_strangle_history_from_db
from nifty_algo.data.strangle_history_db import save_strangle_history

try:
    from fyers_apiv3 import fyersModel

    FYERS_AVAILABLE = True
except Exception:
    fyersModel = None
    FYERS_AVAILABLE = False

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY_INDEX_SYMBOL = "NSE:NIFTY50-INDEX"
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)


def _epoch_to_ist_naive(epoch: int | float) -> datetime:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone(IST).replace(tzinfo=None)


def _in_session(dt: datetime) -> bool:
    t = dt.time().replace(microsecond=0)
    return SESSION_START <= t <= SESSION_END


class FyersHistoricalClient:
    """Fyers client for historical 1m OHLCV (cache-first into db/strangle_data.db)."""

    source_name: Literal["fyers"] = "fyers"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        log_dir: str | None = None,
        index_symbol: str = NIFTY_INDEX_SYMBOL,
    ):
        self.client_id = client_id or ""
        self.access_token = access_token or ""
        self.log_dir = log_dir or "."
        self.index_symbol = index_symbol
        self._model: Any = None
        self._connect_error: str | None = None

    def connect(self) -> bool:
        self._connect_error = None
        if not FYERS_AVAILABLE:
            self._connect_error = "fyers-apiv3 not installed (pip install fyers-apiv3)"
            logger.warning("Fyers historical: %s", self._connect_error)
            return False
        if not self.client_id or not self.access_token:
            self._connect_error = "FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN must be set"
            logger.warning("Fyers historical: %s", self._connect_error)
            return False

        token = self.access_token
        if ":" in token:
            parts = token.split(":", 1)
            token = parts[1] if len(parts) == 2 else token

        try:
            self._model = fyersModel.FyersModel(
                client_id=self.client_id,
                token=token,
                is_async=False,
                log_path=self.log_dir,
            )
            profile = self._model.get_profile()
            if profile.get("code") == 200 or profile.get("s") == "ok":
                logger.info("Fyers historical session OK")
                return True
            self._connect_error = profile.get("message", "profile check failed")
            self._model = None
            logger.warning("Fyers historical login failed: %s", self._connect_error)
            return False
        except Exception as e:
            self._connect_error = str(e).strip() or repr(e)
            self._model = None
            logger.warning("Fyers historical connect failed: %s", self._connect_error)
            return False

    def _require(self) -> None:
        if self._model is None:
            msg = self._connect_error or (
                "Fyers not connected. Set FYERS_CLIENT_ID / FYERS_ACCESS_TOKEN "
                "(run generate_token.py) and restart."
            )
            raise RuntimeError(msg)

    def _history_candles(self, symbol: str, trade_date: date) -> list[OptionCandle]:
        self._require()
        day = trade_date.isoformat()
        data = {
            "symbol": symbol,
            "resolution": "1",
            "date_format": "1",
            "range_from": day,
            "range_to": day,
            "cont_flag": "1",
        }
        try:
            resp = self._model.history(data=data)
        except Exception as e:
            logger.exception("Fyers history failed for %s: %s", symbol, e)
            return []

        if not (resp.get("s") == "ok" or resp.get("code") == 200):
            logger.warning(
                "Fyers history error for %s: %s",
                symbol,
                resp.get("message") or resp,
            )
            return []

        rows = resp.get("candles") or []
        out: list[OptionCandle] = []
        for row in rows:
            try:
                # [epoch, open, high, low, close, volume]
                dt = _epoch_to_ist_naive(row[0])
                if dt.date() != trade_date or not _in_session(dt):
                    continue
                out.append(
                    OptionCandle(
                        datetime=dt,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]) if len(row) > 5 else 0.0,
                    )
                )
            except (TypeError, ValueError, IndexError) as e:
                logger.debug("Skip Fyers candle %s: %s", row, e)
        return out

    def get_option_historical_1m(
        self,
        expiry_date: date,
        strike: int,
        option_type: str,
        trade_date: date,
        stock_code: str = "NIFTY",
        exchange_code: str = "NFO",
    ) -> list[OptionCandle]:
        _ = stock_code, exchange_code
        candidates = build_fyers_option_symbol_candidates(
            expiry_date, int(strike), option_type
        )
        last_sym = candidates[0] if candidates else "?"
        for symbol in candidates:
            last_sym = symbol
            candles = self._history_candles(symbol, trade_date)
            if candles:
                if symbol != candidates[0]:
                    logger.info(
                        "Fyers history used alternate symbol %s (primary was %s)",
                        symbol,
                        candidates[0],
                    )
                return candles
        logger.warning(
            "Fyers returned no 1m bars for %s on %s (tried %s). "
            "Expired contracts may need Fyers expired-FO API; "
            "active symbols need Market Data permission.",
            last_sym,
            trade_date,
            ", ".join(candidates),
        )
        return []

    def get_strangle_historical_1m(
        self,
        expiry_date: date,
        ce_strike: int,
        pe_strike: int,
        trade_date: date,
        stock_code: str = "NIFTY",
        exchange_code: str = "NFO",
    ) -> tuple[list[OptionCandle], list[OptionCandle]]:
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
        if ce and pe:
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
                "Nifty index: %s 1m bars from SQLite %s for %s — skipping Fyers",
                len(cached),
                get_strangle_db_path(),
                trade_date,
            )
            return cached, "database"

        candles = self._history_candles(self.index_symbol, trade_date)
        if candles:
            logger.info(
                "Nifty index: fetched %s 1m bars from Fyers for %s",
                len(candles),
                trade_date,
            )
        if candles and persist:
            save_nifty_index_historical_1m(
                candles,
                symbol=stock_code,
                exchange_code=exchange_code,
                trade_date=trade_date,
                db_path=db_path,
                source=self.source_name,
            )
        return candles, self.source_name
