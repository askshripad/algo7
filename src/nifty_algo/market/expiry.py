from __future__ import annotations
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nifty_algo.brokers.base import BrokerInterface


def get_nearest_expiry(
    broker: BrokerInterface | None = None,
) -> date | None:
    """
    Get nearest option expiry from broker (e.g. contract master).
    No fixed weekday is assumed; holidays and exchange calendar are reflected
    by whatever expiries the broker returns.
    Returns: date or None if broker does not provide expiry.
    """
    if broker is not None and hasattr(broker, "get_nearest_expiry") and callable(getattr(broker, "get_nearest_expiry")):
        return broker.get_nearest_expiry()
    return None

