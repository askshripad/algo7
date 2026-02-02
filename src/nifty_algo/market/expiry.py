from __future__ import annotations
import datetime as dt

try:
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    HAS_PYTZ = True
except Exception:
    IST = None
    HAS_PYTZ = False

def _now_ist() -> dt.datetime:
    """Get current time in IST"""
    if HAS_PYTZ:
        return dt.datetime.now(IST)
    return dt.datetime.now()

def get_nearest_expiry(now:dt.datetime|None=None) -> dt.datetime:
    """
    Nearest expiry is next Tuesday.
    If today is Tuesday and time >= 15:00, use next Tuesday.
    """
    if now is None:
        now = _now_ist()

    # Tuesday = 1 in Python's weekday (Monday=0, Tuesday=1, ..., Sunday=6)
    weekday = now.weekday()
    days_until_tuesday = (1 - weekday) % 7
    candidate = now + dt.timedelta(days=days_until_tuesday)

    if weekday == 1:
        cutoff = now.replace(hour=15, minute=0, second=0, microsecond=0)
        if now >= cutoff:
            candidate = now + dt.timedelta(days=7)

    return candidate.date()

