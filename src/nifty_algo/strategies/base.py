from __future__ import annotations

from abc import ABC, abstractmethod


class Strategy(ABC):
    name: str

    @abstractmethod
    def should_enter(self, ctx) -> bool:
        """Return True if strategy should enter trade."""
        raise NotImplementedError

    @abstractmethod
    def get_entry_strikes(self, all_option_data, nifty_spot, config) -> tuple | None:
        """Return (call_strike, put_strike, call_price, put_price, call_symbol, put_symbol) or None."""
        raise NotImplementedError