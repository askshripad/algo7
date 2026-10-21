"""
Volume Range & OTM Range — broker option-chain accumulation.

See ``api/volume_range_and_otm_range.md`` for the full specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any

VOLUME_START = time(9, 20, 0)
OR_START = time(9, 15, 0)
OR_END = time(9, 20, 0)


def _fmt_range(strike: int | None, premium_crs: float) -> str:
    if strike is None or premium_crs <= 0:
        return "-"
    return f"{int(strike)}:{int(premium_crs)}Cr"


def _max_strike_premium(acc: dict[int, float]) -> tuple[int | None, float]:
    if not acc:
        return None, 0.0
    strike = max(acc, key=acc.get)
    return strike, acc[strike] / 1e7


def _round_atm(spot: float, strike_step: int) -> int:
    return int(round(float(spot) / strike_step) * strike_step)


@dataclass
class VolumeRangeState:
    """Running accumulators for one session (reset at day open)."""

    lot_size: int = 75
    strike_step: int = 50
    call_prev: dict[int, float] = field(default_factory=dict)
    put_prev: dict[int, float] = field(default_factory=dict)
    call_acc: dict[int, float] = field(default_factory=dict)
    put_acc: dict[int, float] = field(default_factory=dict)

    def reset(self) -> None:
        self.call_prev.clear()
        self.put_prev.clear()
        self.call_acc.clear()
        self.put_acc.clear()

    def ingest_chain(
        self,
        options_data: list[dict],
        spot: float,
        *,
        now_time: time | None = None,
    ) -> dict[str, Any]:
        """
        Process one option-chain snapshot (latest row per strike+type wins).

        Returns volume/OTM range labels and strike selections for this tick.
        """
        t = now_time
        if t is not None and t < VOLUME_START:
            atm = _round_atm(spot, self.strike_step)
            return self._empty_result(atm)

        # Latest quote per (strike, side)
        latest: dict[tuple[int, str], dict] = {}
        for opt in options_data:
            strike_raw = opt.get("strike")
            opt_type = (opt.get("option_type") or "").strip().upper()
            if strike_raw is None or opt_type not in ("CE", "PE"):
                continue
            try:
                strike = int(strike_raw)
            except (TypeError, ValueError):
                continue
            latest[(strike, opt_type)] = opt

        for (strike, opt_type), opt in latest.items():
            vol = opt.get("Volume") or opt.get("volume") or 0
            ltp = opt.get("LTP") or opt.get("ltp") or opt.get("Close") or opt.get("close")
            try:
                cv = float(vol) if vol is not None else 0.0
            except (TypeError, ValueError):
                cv = 0.0
            try:
                price = float(ltp) if ltp is not None else 0.0
            except (TypeError, ValueError):
                price = 0.0

            if opt_type == "CE":
                prev = self.call_prev.get(strike, 0.0)
                if prev <= 0:
                    self.call_prev[strike] = cv
                else:
                    inc = cv - prev
                    if inc < 0:
                        inc = 0.0
                    self.call_prev[strike] = cv
                    if inc > 0 and price > 0:
                        self.call_acc[strike] = self.call_acc.get(strike, 0.0) + inc * self.lot_size * price
            else:
                prev = self.put_prev.get(strike, 0.0)
                if prev <= 0:
                    self.put_prev[strike] = cv
                else:
                    inc = cv - prev
                    if inc < 0:
                        inc = 0.0
                    self.put_prev[strike] = cv
                    if inc > 0 and price > 0:
                        self.put_acc[strike] = self.put_acc.get(strike, 0.0) + inc * self.lot_size * price

        atm = _round_atm(spot, self.strike_step)
        cs, cc = _max_strike_premium(self.call_acc)
        ps, pc = _max_strike_premium(self.put_acc)

        call_otm = {s: v for s, v in self.call_acc.items() if s > atm}
        put_otm = {s: v for s, v in self.put_acc.items() if s < atm}
        cos, coc = _max_strike_premium(call_otm)
        pos, poc = _max_strike_premium(put_otm)

        return {
            "atm_strike": atm,
            "call_volume_range": _fmt_range(cs, cc),
            "put_volume_range": _fmt_range(ps, pc),
            "call_otm_volume_range": _fmt_range(cos, coc),
            "put_otm_volume_range": _fmt_range(pos, poc),
            "vr_ce_strike": cs,
            "vr_pe_strike": ps,
            "otm_ce_strike": cos,
            "otm_pe_strike": pos,
            "call_volume_range_premium_crs": cc,
            "put_volume_range_premium_crs": pc,
            "call_otm_premium_crs": coc,
            "put_otm_premium_crs": poc,
        }

    def _empty_result(self, atm: int) -> dict[str, Any]:
        return {
            "atm_strike": atm,
            "call_volume_range": "-",
            "put_volume_range": "-",
            "call_otm_volume_range": "-",
            "put_otm_volume_range": "-",
            "vr_ce_strike": None,
            "vr_pe_strike": None,
            "otm_ce_strike": None,
            "otm_pe_strike": None,
            "call_volume_range_premium_crs": 0.0,
            "put_volume_range_premium_crs": 0.0,
            "call_otm_premium_crs": 0.0,
            "put_otm_premium_crs": 0.0,
        }
