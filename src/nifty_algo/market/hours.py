from __future__ import annotations

import datetime as dt
import logging
import time

try:
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    HAS_PYTZ = True
except Exception:
    IST = None
    HAS_PYTZ = False

logger = logging.getLogger(__name__)

def _now_ist() -> dt.datetime:
    """Get current time in IST"""
    if HAS_PYTZ:
        return dt.datetime.now(IST)
    
    # fallback to naive datetime
    return dt.datetime.now()


def now_ist() -> dt.datetime:
    """Public alias: current time in Asia/Kolkata when pytz is installed, else naive local."""
    return _now_ist()


def is_within_entry_cutoff(
    cutoff_hour: int,
    cutoff_minute: int,
    now: dt.datetime | None = None,
) -> bool:
    """
    True if current IST time is on or before the end of the cutoff minute (inclusive).

    Used to allow new strategy entries only until e.g. 9:20 IST; after that, keep
    fetching data but do not open new positions. Does not apply to exit monitoring
    of a position already opened earlier in the session.
    """
    now = now or _now_ist()
    if now.weekday() > 4:
        return False
    end = now.replace(hour=cutoff_hour, minute=cutoff_minute, second=59, microsecond=999999)
    return now <= end


def is_market_open(now:dt.datetime|None=None) -> bool:
    """Check if NSE market is currently open (9:15 AM - 3:30 PM IST)"""
    if now is None:
        now = _now_ist()
    
    if not HAS_PYTZ and now.tzinfo is None:
        # Warning once per call; keep it simple
        print("Warning: pytz not available; using naive local time for IST.")

    # Weekday: Mon=0 ... Fri=4
    if now.weekday() > 4:
        return False

    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end

def _next_trading_day(d: dt.datetime) -> dt.datetime:
    # Move to next trading day (Mon-Fri)
    nxt = d + dt.timedelta(days=1)
    while nxt.weekday() > 4:
        nxt += dt.timedelta(days=1)
    return nxt

def wait_for_market_open(check_interval: int = 10) -> None:
    """
    Block until market opens. If after 15:30, wait for next trading day.
    """
    fast_polling_logged = False
    while True:
        now = _now_ist()

        if is_market_open(now):
            return

        # Before open on a trading day
        if now.weekday() <= 4:
            open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
            close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)

            if now < open_time:
                remaining_seconds = (open_time - now).total_seconds()
                minutes = remaining_seconds / 60
                logger.info("Market opens at 9:15 AM IST")
                logger.info("Current time: %s (IST)", now.strftime("%H:%M:%S"))
                logger.info("Waiting %.1f minutes until market opens...", minutes)
                logger.info("(Press Ctrl+C to exit)")
                if remaining_seconds <= 120:
                    if not fast_polling_logged:
                        logger.info("Within 2 minutes of open. Polling every 200ms.")
                        fast_polling_logged = True
                    time.sleep(0.5)
                else:
                    time.sleep(check_interval)
                continue

            # After close, move to next trading day
            if now > close_time:
                next_day = _next_trading_day(now)
                target = next_day.replace(hour=9, minute=15, second=0, microsecond=0)
                sleep_seconds = max(1, int((target - now).total_seconds()))
                minutes = sleep_seconds / 60
                logger.info("Market closed. Next open at 9:15 AM IST")
                logger.info("Current time: %s (IST)", now.strftime("%H:%M:%S"))
                logger.info("Waiting %.1f minutes until market opens...", minutes)
                logger.info("(Press Ctrl+C to exit)")
                time.sleep(min(check_interval, sleep_seconds))
                continue # uncomment shripad

        # Weekend: wait until next trading day
        next_day = _next_trading_day(now)
        target = next_day.replace(hour=9, minute=15, second=0, microsecond=0)
        sleep_seconds = max(1, int((target - now).total_seconds()))
        minutes = sleep_seconds / 60
        logger.info("Market closed (weekend). Next open at 9:15 AM IST")
        logger.info("Current time: %s (IST)", now.strftime("%H:%M:%S"))
        logger.info("Waiting %.1f minutes until market opens...", minutes)
        logger.info("(Press Ctrl+C to exit)")
        time.sleep(min(check_interval, sleep_seconds))