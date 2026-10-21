# volume_normalizer.py
from dataclasses import dataclass, field
from typing import Dict, Tuple

@dataclass
class VolumeNormalizer:
    # key: (symbol, trade_date) -> last raw volume
    last_raw: Dict[Tuple[str, str], float] = field(default_factory=dict)

    def normalize(self, symbol: str, trade_date: str, raw_volume: float | int | None) -> tuple[float, float]:
        """
        Returns (volume_raw, volume_delta)
        """
        try:
            raw = float(raw_volume) if raw_volume is not None else 0.0
        except Exception:
            raw = 0.0

        key = (symbol, trade_date)
        prev = self.last_raw.get(key)

        if prev is None:
            delta = raw
        elif raw >= prev:
            delta = raw - prev
        else:
            # reset/day rollover/broker anomaly
            delta = raw

        self.last_raw[key] = raw
        return raw, max(delta, 0.0)