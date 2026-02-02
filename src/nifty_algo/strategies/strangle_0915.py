from __future__ import annotations

from nifty_algo.core.strikes import find_entry_strikes
from nifty_algo.strategies.base import Strategy


class Strangle0915Strategy(Strategy):
    name = "Strangle_0915"

    def should_enter(self, ctx) -> bool:
        return True

    def get_entry_strikes(self, all_option_data, nifty_spot, config) -> tuple | None:
        return find_entry_strikes(
            all_option_data,
            nifty_spot,
            config.MAX_TOTAL_PREMIUM,
            min_premium=None,
            max_premium=None,
        )