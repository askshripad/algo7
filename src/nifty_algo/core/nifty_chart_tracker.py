"""
Nifty Chart Tracker — extended live/historical state for /niftychart.

Tracks:
- Volume-range strangle (dynamic CE/PE from accumulated premium)
- OTM-range strangle (dynamic)
- Fixed offset strangle (ATM ± STRANGLE_ATM_OFFSET_POINTS)
- Opening range high/low (09:15–09:20 IST) on Nifty spot
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from nifty_algo.core.volume_range import OR_END, OR_START, VolumeRangeState

logger = logging.getLogger(__name__)


def _round_to_atm(spot: float, interval: int) -> int:
    return int(round(float(spot) / interval) * interval)


def _strikes_from_spot(spot: float, offset: int, interval: int) -> tuple[int, int]:
    atm = _round_to_atm(spot, interval)
    return atm + offset, atm - offset


def _valid_price(v: float | None) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _valid_strike(v: int | None) -> int | None:
    if v is None:
        return None
    try:
        i = int(v)
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def _find_leg(options_data: list[dict], strike: int | None, opt_type: str) -> tuple[float | None, float]:
    if strike is None:
        return None, 0.0
    for opt in options_data:
        try:
            si = int(opt.get("strike"))
        except (TypeError, ValueError):
            continue
        if si == int(strike) and (opt.get("option_type") or "").upper() == opt_type:
            ltp = opt.get("LTP") or opt.get("ltp")
            vol = opt.get("Volume") or opt.get("volume") or 0
            try:
                price = float(ltp) if ltp is not None else None
            except (TypeError, ValueError):
                price = None
            try:
                vol_f = float(vol) if vol is not None else 0.0
            except (TypeError, ValueError):
                vol_f = 0.0
            return price, vol_f
    return None, 0.0


@dataclass
class NiftyChartSnapshot:
    timestamp: datetime
    spot_price: float
    # Volume range strangle
    vr_ce_strike: int | None
    vr_pe_strike: int | None
    vr_ce_price: float | None
    vr_pe_price: float | None
    vr_total_premium: float | None
    vr_ce_volume: float
    vr_pe_volume: float
    call_volume_range: str
    put_volume_range: str
    # OTM range
    otm_ce_strike: int | None
    otm_pe_strike: int | None
    otm_ce_price: float | None
    otm_pe_price: float | None
    otm_total_premium: float | None
    call_otm_volume_range: str
    put_otm_volume_range: str
    # Fixed offset strangle
    offset_ce_strike: int
    offset_pe_strike: int
    offset_ce_price: float | None
    offset_pe_price: float | None
    offset_total_premium: float | None
    offset_ce_volume: float
    offset_pe_volume: float
    # Opening range (Nifty)
    or_high: float | None
    or_low: float | None
    atm_strike: int


@dataclass
class NiftyChartTracker:
    """Session tracker for /niftychart (singleton)."""

    atm_offset_points: int = 100
    strike_interval: int = 50
    lot_size: int = 75

    _volume_state: VolumeRangeState = field(default_factory=VolumeRangeState)
    _history: list[NiftyChartSnapshot] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # Opening range accumulators (09:15–09:20)
    _or_high: float | None = None
    _or_low: float | None = None
    _or_frozen: bool = False

    # Latest offset strikes (fixed at first spot)
    _offset_ce: int | None = None
    _offset_pe: int | None = None

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._volume_state.reset()
            self._or_high = None
            self._or_low = None
            self._or_frozen = False
            self._offset_ce = None
            self._offset_pe = None
            logger.info("Nifty chart tracker reset")

    def _backfill_or_from_db(self) -> None:
        """If algo started after 09:20, recover OR from today's nifty_spot snapshots."""
        if self._or_high is not None and self._or_low is not None:
            return
        try:
            import sqlite3
            from datetime import date as date_cls
            from pathlib import Path

            from nifty_algo.config import config

            log_dir = Path(config.LOG_DIR)
            if not log_dir.is_absolute():
                root = Path(__file__).resolve().parents[3]
                log_dir = (root / log_dir).resolve()
            db_path = log_dir / config.DATABASE_NAME
            if not db_path.is_file():
                return
            td = date_cls.today().isoformat()
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT timestamp, COALESCE(nifty_spot, spot_price) AS spot
                    FROM nifty_spot
                    WHERE date = ?
                      AND timestamp >= '09:15:00' AND timestamp < '09:20:00'
                    ORDER BY timestamp ASC
                    """,
                    (td,),
                ).fetchall()
            finally:
                conn.close()
            spots = [float(r[1]) for r in rows if r[1] is not None and float(r[1]) > 0]
            if not spots:
                return
            self._or_high = max(spots)
            self._or_low = min(spots)
            self._or_frozen = True
            logger.info("OR backfilled from DB: high=%.2f low=%.2f (%d spots)", self._or_high, self._or_low, len(spots))
        except Exception as e:
            logger.debug("OR DB backfill: %s", e)

    def _update_opening_range(self, spot: float, now: datetime) -> None:
        t = now.time().replace(microsecond=0)
        if t >= OR_END:
            if not self._or_frozen:
                self._or_frozen = True
                if self._or_high is None or self._or_low is None:
                    self._backfill_or_from_db()
            return
        if self._or_frozen or spot <= 0:
            return
        if t < OR_START:
            return
        self._or_high = spot if self._or_high is None else max(self._or_high, spot)
        self._or_low = spot if self._or_low is None else min(self._or_low, spot)

    def update(
        self,
        options_data: list[dict],
        spot_price: float,
        *,
        record_history: bool = True,
        now: datetime | None = None,
    ) -> NiftyChartSnapshot | None:
        with self._lock:
            if not spot_price or spot_price <= 0:
                return None

            now = now or datetime.now()
            self._update_opening_range(spot_price, now)

            if self._offset_ce is None:
                self._offset_ce, self._offset_pe = _strikes_from_spot(
                    spot_price, self.atm_offset_points, self.strike_interval
                )

            ranges = self._volume_state.ingest_chain(
                options_data, spot_price, now_time=now.time()
            )
            atm = ranges["atm_strike"]
            vr_ce_s = _valid_strike(ranges["vr_ce_strike"])
            vr_pe_s = _valid_strike(ranges["vr_pe_strike"])
            otm_ce_s = _valid_strike(ranges["otm_ce_strike"])
            otm_pe_s = _valid_strike(ranges["otm_pe_strike"])

            vr_ce_p, vr_ce_v = _find_leg(options_data, vr_ce_s, "CE")
            vr_pe_p, vr_pe_v = _find_leg(options_data, vr_pe_s, "PE")
            otm_ce_p, _ = _find_leg(options_data, otm_ce_s, "CE")
            otm_pe_p, _ = _find_leg(options_data, otm_pe_s, "PE")
            off_ce_p, off_ce_v = _find_leg(options_data, self._offset_ce, "CE")
            off_pe_p, off_pe_v = _find_leg(options_data, self._offset_pe, "PE")

            vr_ce_p = _valid_price(vr_ce_p)
            vr_pe_p = _valid_price(vr_pe_p)
            otm_ce_p = _valid_price(otm_ce_p)
            otm_pe_p = _valid_price(otm_pe_p)
            off_ce_p = _valid_price(off_ce_p)
            off_pe_p = _valid_price(off_pe_p)

            vr_total = (vr_ce_p + vr_pe_p) if vr_ce_p is not None and vr_pe_p is not None else None
            vr_total = _valid_price(vr_total)
            otm_total = (otm_ce_p + otm_pe_p) if otm_ce_p is not None and otm_pe_p is not None else None
            otm_total = _valid_price(otm_total)
            off_total = (off_ce_p + off_pe_p) if off_ce_p is not None and off_pe_p is not None else None
            off_total = _valid_price(off_total)
            spot_price = _valid_price(spot_price) or spot_price  # already guarded >0 at entry

            snap = NiftyChartSnapshot(
                timestamp=now,
                spot_price=spot_price,
                vr_ce_strike=vr_ce_s,
                vr_pe_strike=vr_pe_s,
                vr_ce_price=vr_ce_p,
                vr_pe_price=vr_pe_p,
                vr_total_premium=vr_total,
                vr_ce_volume=vr_ce_v,
                vr_pe_volume=vr_pe_v,
                call_volume_range=ranges["call_volume_range"],
                put_volume_range=ranges["put_volume_range"],
                otm_ce_strike=otm_ce_s,
                otm_pe_strike=otm_pe_s,
                otm_ce_price=otm_ce_p,
                otm_pe_price=otm_pe_p,
                otm_total_premium=otm_total,
                call_otm_volume_range=ranges["call_otm_volume_range"],
                put_otm_volume_range=ranges["put_otm_volume_range"],
                offset_ce_strike=self._offset_ce,
                offset_pe_strike=self._offset_pe,
                offset_ce_price=off_ce_p,
                offset_pe_price=off_pe_p,
                offset_total_premium=off_total,
                offset_ce_volume=off_ce_v,
                offset_pe_volume=off_pe_v,
                or_high=self._or_high,
                or_low=self._or_low,
                atm_strike=atm,
            )

            if record_history:
                self._history.append(snap)
                if len(self._history) > 12000:
                    self._history = self._history[-12000:]

            return snap

    def snapshot_to_dict(self, s: NiftyChartSnapshot) -> dict[str, Any]:
        return self._snap_to_dict(s)

    def _snap_to_dict(self, s: NiftyChartSnapshot) -> dict[str, Any]:
        or_h = _valid_price(s.or_high)
        or_l = _valid_price(s.or_low)
        vr_ce_p = _valid_price(s.vr_ce_price)
        vr_pe_p = _valid_price(s.vr_pe_price)
        vr_total = _valid_price(s.vr_total_premium)
        return {
            "timestamp": s.timestamp.isoformat(),
            "spot_price": _valid_price(s.spot_price),
            "atm_strike": _valid_strike(s.atm_strike),
            "or_high": or_h,
            "or_low": or_l,
            # Volume range
            "vr_ce_strike": _valid_strike(s.vr_ce_strike),
            "vr_pe_strike": _valid_strike(s.vr_pe_strike),
            "vr_ce_price": vr_ce_p,
            "vr_pe_price": vr_pe_p,
            "vr_total_premium": vr_total,
            "vr_ce_volume": s.vr_ce_volume,
            "vr_pe_volume": s.vr_pe_volume,
            "vr_volume_diff": s.vr_ce_volume - s.vr_pe_volume,
            "call_volume_range": s.call_volume_range,
            "put_volume_range": s.put_volume_range,
            # OTM range
            "otm_ce_strike": _valid_strike(s.otm_ce_strike),
            "otm_pe_strike": _valid_strike(s.otm_pe_strike),
            "otm_ce_price": _valid_price(s.otm_ce_price),
            "otm_pe_price": _valid_price(s.otm_pe_price),
            "otm_total_premium": _valid_price(s.otm_total_premium),
            "call_otm_volume_range": s.call_otm_volume_range,
            "put_otm_volume_range": s.put_otm_volume_range,
            # Offset strangle
            "offset_ce_strike": _valid_strike(s.offset_ce_strike),
            "offset_pe_strike": _valid_strike(s.offset_pe_strike),
            "offset_ce_price": _valid_price(s.offset_ce_price),
            "offset_pe_price": _valid_price(s.offset_pe_price),
            "offset_total_premium": _valid_price(s.offset_total_premium),
            "offset_ce_volume": s.offset_ce_volume,
            "offset_pe_volume": s.offset_pe_volume,
            "offset_volume_diff": s.offset_ce_volume - s.offset_pe_volume,
            "put_flow_signal": (
                vr_pe_p is not None
                and vr_ce_p is not None
                and vr_pe_p > vr_ce_p
            ),
        }

    def get_current(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._history:
                return None
            return self._snap_to_dict(self._history[-1])

    def get_history(self, limit: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            n = len(self._history)
            if limit < 0:
                k = min(n, -limit)
                seq = self._history[-k:] if k else []
            elif limit == 0:
                seq = self._history[:]
            else:
                seq = self._history[: min(n, limit)]
            return [self._snap_to_dict(s) for s in seq]

    def get_or_levels(self) -> dict[str, float | None]:
        with self._lock:
            if self._or_high is None or self._or_low is None:
                self._backfill_or_from_db()
            return {
                "or_high": _valid_price(self._or_high),
                "or_low": _valid_price(self._or_low),
            }


_tracker: NiftyChartTracker | None = None


def get_nifty_chart_tracker() -> NiftyChartTracker:
    global _tracker
    if _tracker is None:
        try:
            from nifty_algo.config import config

            _tracker = NiftyChartTracker(
                atm_offset_points=int(getattr(config, "STRANGLE_ATM_OFFSET_POINTS", 100)),
                strike_interval=int(getattr(config, "STRIKE_INTERVAL", 50)),
                lot_size=int(getattr(config, "ORDER_QTY", 75)),
            )
        except Exception:
            _tracker = NiftyChartTracker()
        logger.info("Nifty chart tracker created")
    return _tracker
