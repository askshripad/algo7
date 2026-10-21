from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from nifty_algo.config import config

if TYPE_CHECKING:
    from nifty_algo.brokers.base import BrokerInterface

logger = logging.getLogger(__name__)


def _clamp_weekday(w: int) -> int:
    if 0 <= w <= 6:
        return w
    return 1  # default Tuesday


def next_weekly_expiry_on_or_after(d: date, weekday: int) -> date:
    """Next calendar date on `weekday` (Mon=0 … Sun=6) on or after `d`."""
    w = _clamp_weekday(weekday)
    days_ahead = w - d.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def get_nearest_expiry(
    broker: BrokerInterface | None = None,
) -> date | None:
    """
    Resolve nearest Nifty option expiry (single source for live algo + charts):

    1. ``NIFTY_EXPIRY_DATE`` in ``.env`` (YYYY-MM-DD) if set and on/after today.
    2. Else ``broker.get_nearest_expiry()`` when the broker provides it (e.g. Angel contract master).
    3. Else next occurrence of ``NIFTY_WEEKLY_EXPIRY_WEEKDAY`` (default 1 = Tuesday).

    Fyers CE/PE tickers are built from this date automatically (weekly O/N/D vs monthly MMM).
    You normally only need to set ``NIFTY_EXPIRY_DATE`` — not ``FYERS_OPTION_SYMBOL_STYLE``.
    """
    today = date.today()

    od = config.NIFTY_EXPIRY_DATE
    if od is not None:
        if od >= today:
            return od
        logger.warning(
            "NIFTY_EXPIRY_DATE=%s is before today; ignoring and using broker/calendar",
            od.isoformat(),
        )

    if broker is not None and hasattr(broker, "get_nearest_expiry") and callable(
        getattr(broker, "get_nearest_expiry")
    ):
        d = broker.get_nearest_expiry()
        if d is not None:
            return d

    return next_weekly_expiry_on_or_after(today, config.NIFTY_WEEKLY_EXPIRY_WEEKDAY)


def expiry_resolution_info(broker: BrokerInterface | None = None) -> dict[str, Any]:
    """
    Expiry metadata for UIs / debugging.

    ``nifty_expiry_date_env`` is the raw ``.env`` override (even if in the past — useful for backtest defaults).
    ``resolved_expiry`` is what live trading/charts use today via ``get_nearest_expiry``.
    """
    from nifty_algo.brokers.fyers_symbol_format import is_likely_nifty_monthly_expiry

    env_exp = config.NIFTY_EXPIRY_DATE
    resolved = get_nearest_expiry(broker)
    source = "calendar_weekday"
    if env_exp is not None and env_exp >= date.today():
        source = "NIFTY_EXPIRY_DATE"
    elif broker is not None and resolved is not None and env_exp is None:
        source = "broker"
    elif env_exp is not None and env_exp < date.today():
        source = "calendar_weekday_env_expired"

    sample = None
    if resolved is not None:
        from nifty_algo.brokers.fyers_symbol_format import build_fyers_option_symbol

        sample = build_fyers_option_symbol(resolved, 25000, "CE")

    return {
        "nifty_expiry_date_env": env_exp.isoformat() if env_exp else None,
        "resolved_expiry": resolved.isoformat() if resolved else None,
        "source": source,
        "is_likely_monthly": (
            is_likely_nifty_monthly_expiry(resolved) if resolved is not None else None
        ),
        "fyers_symbol_style_config": getattr(config, "FYERS_OPTION_SYMBOL_STYLE", "auto"),
        "fyers_symbol_auto": True,
        "sample_fyers_ce_25000": sample,
        "hint": (
            "Set only NIFTY_EXPIRY_DATE for holiday/special weeks. "
            "Fyers weekly vs monthly ticker is inferred from that expiry date."
        ),
    }
