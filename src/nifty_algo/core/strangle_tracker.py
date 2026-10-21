"""
Strangle Tracker - Tracks the algo's selected strangle for chart visualization.

A strangle has DIFFERENT strikes for CE and PE (unlike straddle which has same strike).
This module tracks the exact strikes the trading algo selects.
"""

from __future__ import annotations
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StrangleSnapshot:
    """Single point in time for the strangle."""
    timestamp: datetime
    ce_strike: int
    pe_strike: int
    ce_price: float
    pe_price: float
    total_premium: float
    spot_price: float
    ce_volume: float = 0.0
    pe_volume: float = 0.0


def _round_to_atm(spot: float) -> int:
    """Round Nifty spot to nearest strike interval (ATM strike)."""
    try:
        from nifty_algo.config import config
        interval = getattr(config, "STRIKE_INTERVAL", 50)
    except Exception:
        interval = 50
    return int(round(spot / interval) * interval)


def _strikes_from_spot(spot: float, atm_offset_points: int) -> tuple[int, int]:
    """
    Same rule as backtest: ATM = nearest STRIKE_INTERVAL to spot;
    CE = ATM + atm_offset_points, PE = ATM − atm_offset_points (no extra rounding).
    """
    atm = _round_to_atm(spot)
    return atm + atm_offset_points, atm - atm_offset_points


@dataclass 
class StrangleTracker:
    """
    Tracks the strangle for chart visualization.
    
    When algo has not set strikes, we use fixed rule (same as backtest):
    - ATM = nearest STRIKE_INTERVAL to spot
    - CE = ATM + STRANGLE_ATM_OFFSET_POINTS, PE = ATM − STRANGLE_ATM_OFFSET_POINTS
    
    The algo can override by calling set_strikes() when it finds entry.
    """
    max_premium: float = 112.0  # From config.MAX_TOTAL_PREMIUM
    atm_offset_points: int = 100  # config.STRANGLE_ATM_OFFSET_POINTS (same as backtest)
    
    # Selected strangle (set by algo or auto from spot)
    _ce_strike: int | None = None
    _pe_strike: int | None = None
    _selected_at: datetime | None = None
    _ce_symbol: str | None = None
    _pe_symbol: str | None = None
    
    # Price history for chart
    _history: list[StrangleSnapshot] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    
    # Latest prices and volume (for real-time display)
    _latest_ce_price: float | None = None
    _latest_pe_price: float | None = None
    _latest_ce_volume: float = 0.0
    _latest_pe_volume: float = 0.0
    _latest_spot: float | None = None
    
    def set_strikes(self, ce_strike: int, pe_strike: int, 
                    ce_symbol: str = None, pe_symbol: str = None):
        """
        Set the strikes to track (called by algo when entry is found).
        If CHART_STRANGLE_ONLY is True, we do not overwrite: chart keeps showing
        the display strangle (ATM±STRANGLE_ATM_OFFSET_POINTS), not algo entry strikes.
        """
        with self._lock:
            try:
                from nifty_algo.config import config
                if getattr(config, "CHART_STRANGLE_ONLY", True):
                    return  # Chart shows display strangle only; ignore algo entry strikes
            except Exception:
                pass
            if self._ce_strike is None:
                self._ce_strike = ce_strike
                self._pe_strike = pe_strike
                self._selected_at = datetime.now()
                self._ce_symbol = ce_symbol
                self._pe_symbol = pe_symbol
                logger.info("Strangle tracker: CE=%d PE=%d selected by algo", 
                           ce_strike, pe_strike)
            elif self._ce_strike != ce_strike or self._pe_strike != pe_strike:
                logger.info("Strangle tracker: Changed CE %d->%d, PE %d->%d", 
                           self._ce_strike, ce_strike, self._pe_strike, pe_strike)
                self._ce_strike = ce_strike
                self._pe_strike = pe_strike
                self._selected_at = datetime.now()
                self._ce_symbol = ce_symbol
                self._pe_symbol = pe_symbol
    
    def update(
        self,
        options_data: list[dict],
        spot_price: float,
        *,
        record_history: bool = True,
    ) -> StrangleSnapshot | None:
        """
        Update with new market data. Records price history for the selected strangle when
        record_history=True (main feed via cache.publish_options). API/WS read paths should
        use record_history=False to refresh latest prices without appending duplicate points.

        If no strikes are set by algo, we use fixed rule: CE = ATM+offset, PE = ATM−offset
        (config STRANGLE_ATM_OFFSET_POINTS, same as backtest).
        So chart always shows this strangle once we have spot; prices fill in when options data has these strikes.

        Returns: Latest snapshot when we have both CE and PE prices, else None (but strikes may be set).
        """
        with self._lock:
            # If no strikes selected by algo, set from spot: CE = ATM+offset, PE = ATM−offset
            if self._ce_strike is None and spot_price and spot_price > 0:
                try:
                    from nifty_algo.config import config
                    off = max(0, int(getattr(config, "STRANGLE_ATM_OFFSET_POINTS", 100)))
                    use_preopen = bool(
                        getattr(config, "USE_PREOPEN_PRICE_FOR_STRIKE_SELECTION", False)
                    )
                    preopen = getattr(config, "NIFTY_PREOPEN_PRICE", None)
                    strike_spot = (
                        float(preopen)
                        if use_preopen and preopen is not None and float(preopen) > 0
                        else spot_price
                    )
                except Exception:
                    off = self.atm_offset_points
                    strike_spot = spot_price
                self._ce_strike, self._pe_strike = _strikes_from_spot(strike_spot, off)
                self._selected_at = datetime.now()
                self._latest_spot = spot_price
                logger.info(
                    "Strangle tracker: Auto-selected CE=%d PE=%d (atm_offset=%d, strike_spot=%.2f, live_spot=%.2f)",
                    self._ce_strike,
                    self._pe_strike,
                    off,
                    strike_spot,
                    spot_price,
                )

            if self._ce_strike is None or self._pe_strike is None:
                return None
            
            # Always update latest spot
            self._latest_spot = spot_price
            
            # Find current prices and volume for selected strikes
            ce_price = None
            pe_price = None
            ce_volume = 0.0
            pe_volume = 0.0

            for opt in options_data:
                opt_strike = opt.get("strike")
                if opt_strike is None:
                    continue

                try:
                    strike_int = int(opt_strike)
                except (ValueError, TypeError):
                    continue

                opt_type = opt.get("option_type")
                ltp = opt.get("LTP") or opt.get("ltp")  # broker may use LTP or ltp
                vol = opt.get("Volume") or opt.get("volume")
                try:
                    vol_f = float(vol) if vol is not None else 0.0
                except (ValueError, TypeError):
                    vol_f = 0.0

                if strike_int == self._ce_strike and opt_type == "CE":
                    if ltp is not None:
                        ce_price = float(ltp)
                    ce_volume = vol_f
                elif strike_int == self._pe_strike and opt_type == "PE":
                    if ltp is not None:
                        pe_price = float(ltp)
                    pe_volume = vol_f

            # Update latest prices and volume even if only one leg (so get_current() can show partial)
            if ce_price is not None:
                self._latest_ce_price = ce_price
            if pe_price is not None:
                self._latest_pe_price = pe_price
            self._latest_ce_volume = ce_volume
            self._latest_pe_volume = pe_volume

            if ce_price is None or pe_price is None:
                return None

            # Create snapshot when we have both
            snapshot = StrangleSnapshot(
                timestamp=datetime.now(),
                ce_strike=self._ce_strike,
                pe_strike=self._pe_strike,
                ce_price=ce_price,
                pe_price=pe_price,
                total_premium=ce_price + pe_price,
                spot_price=spot_price,
                ce_volume=ce_volume,
                pe_volume=pe_volume,
            )

            if record_history:
                self._history.append(snapshot)
                # Keep a full session window (do not trim to last-N only: that drops market open).
                if len(self._history) > 12000:
                    self._history = self._history[-12000:]
            
            return snapshot
    
    def get_current(self) -> dict[str, Any] | None:
        """Get current strangle info for API (strikes + prices when available)."""
        with self._lock:
            if self._ce_strike is None or self._pe_strike is None:
                return None
            
            total = 0.0
            if self._latest_ce_price is not None and self._latest_pe_price is not None:
                total = self._latest_ce_price + self._latest_pe_price
            
            return {
                "ce_strike": self._ce_strike,
                "pe_strike": self._pe_strike,
                "selected_at": self._selected_at.isoformat() if self._selected_at else None,
                "ce_price": self._latest_ce_price,
                "pe_price": self._latest_pe_price,
                "ce_volume": self._latest_ce_volume,
                "pe_volume": self._latest_pe_volume,
                "volume_diff": self._latest_ce_volume - self._latest_pe_volume,
                "total_premium": total if total else None,
                "spot_price": self._latest_spot,
                "max_premium_target": self.max_premium,
                "ce_symbol": self._ce_symbol,
                "pe_symbol": self._pe_symbol,
            }
    
    def get_history(self, limit: int = 0) -> list[dict[str, Any]]:
        """
        Price history for chart, oldest → newest.

        - ``limit == 0``: all retained points (full session, for charts).
        - ``limit > 0``: at most that many points **from session open** (prefix).
        - ``limit < 0``: last ``-limit`` points only (e.g. ``-1`` = latest bar for WS snapshot).
        """
        with self._lock:
            n = len(self._history)
            if limit < 0:
                k = min(n, -limit)
                seq = self._history[-k:] if k else []
            elif limit == 0:
                seq = self._history[:]
            else:
                seq = self._history[: min(n, limit)]
            return [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "ce_strike": s.ce_strike,
                    "pe_strike": s.pe_strike,
                    "ce_price": s.ce_price,
                    "pe_price": s.pe_price,
                    "ce_volume": getattr(s, "ce_volume", 0.0),
                    "pe_volume": getattr(s, "pe_volume", 0.0),
                    "volume_diff": getattr(s, "ce_volume", 0.0) - getattr(s, "pe_volume", 0.0),
                    "total_premium": s.total_premium,
                    "spot_price": s.spot_price,
                }
                for s in seq
            ]
    
    def reset(self):
        """Reset tracker (for new trading day)."""
        with self._lock:
            self._ce_strike = None
            self._pe_strike = None
            self._selected_at = None
            self._ce_symbol = None
            self._pe_symbol = None
            self._history.clear()
            self._latest_ce_price = None
            self._latest_pe_price = None
            self._latest_ce_volume = 0.0
            self._latest_pe_volume = 0.0
            self._latest_spot = None
            logger.info("Strangle tracker reset")


# Singleton instance
_tracker: StrangleTracker | None = None

def get_strangle_tracker() -> StrangleTracker:
    """Get or create the strangle tracker singleton."""
    global _tracker
    if _tracker is None:
        try:
            from nifty_algo.config import config
            max_premium = config.MAX_TOTAL_PREMIUM
            atm_off = int(getattr(config, "STRANGLE_ATM_OFFSET_POINTS", 100))
        except Exception:
            max_premium = 112.0
            atm_off = 100

        _tracker = StrangleTracker(max_premium=max_premium, atm_offset_points=atm_off)
        logger.info(
            "Strangle tracker created (max_premium=%.2f, STRANGLE_ATM_OFFSET_POINTS=%d)",
            max_premium,
            atm_off,
        )
    return _tracker


def set_algo_strikes(ce_strike: int, pe_strike: int, 
                     ce_symbol: str = None, pe_symbol: str = None):
    """
    Convenience function for algo to set the tracked strikes.
    Call this when the algo finds entry strikes.
    """
    tracker = get_strangle_tracker()
    tracker.set_strikes(ce_strike, pe_strike, ce_symbol, pe_symbol)
