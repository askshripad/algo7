from __future__ import annotations
import logging
import threading
import time
import queue
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

@dataclass
class MarketCache:
    """ Thread-safe in memory cache for market data """
    _options_data: deque = field(default_factory=lambda: deque(maxlen=1000))
    _spot_price: float | None = None
    _spot_timestamp: datetime | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _write_queue: queue.Queue = field(default_factory=queue.Queue)
    _subscribers: list[callable] = field(default_factory=list)
    _bg_thread: threading.Thread | None = None
    _running: bool = False
    # Throttle: only update cache/UI/DB at most every N seconds (config.UI_UPDATE_INTERVAL)
    _last_publish_time: float = 0.0

    def _get_update_interval_sec(self) -> int:
        """Seconds between cache/UI/DB updates (configurable via UI_UPDATE_INTERVAL)."""
        try:
            from nifty_algo.config import config
            return max(1, int(config.UI_UPDATE_INTERVAL))
        except Exception:
            return 60

    def publish_options(self, data: list[dict], nifty_spot: float, atm_strike: int = 0):
        """
        Publish from algo. Updates cache, UI (WebSocket), DB and strangle tracker
        at most every UI_UPDATE_INTERVAL seconds to avoid overload.
        """
        interval_sec = self._get_update_interval_sec()
        now = time.time()
        with self._lock:
            if self._last_publish_time > 0 and (now - self._last_publish_time) < interval_sec:
                # Throttle: skip this update
                return
            self._last_publish_time = now

            ts = datetime.now()
            self._spot_price = nifty_spot
            self._spot_timestamp = ts
            for row in data:
                row["_cache_ts"] = ts
                self._options_data.append(row)
            logger.info("Cache publish: %d rows | spot=%.2f | total=%d (interval=%ds)", 
                       len(data), nifty_spot, len(self._options_data), interval_sec)

        # Update strangle tracker (for chart visualization)
        try:
            from nifty_algo.core.strangle_tracker import get_strangle_tracker
            tracker = get_strangle_tracker()
            all_data = list(self._options_data)
            tracker.update(all_data, nifty_spot)
        except Exception as e:
            logger.debug("Strangle tracker update failed: %s", e)

        # Nifty chart tracker (volume range, OTM, opening range)
        try:
            from nifty_algo.core.nifty_chart_tracker import get_nifty_chart_tracker
            nct = get_nifty_chart_tracker()
            nct.update(list(self._options_data), nifty_spot)
        except Exception as e:
            logger.debug("Nifty chart tracker update failed: %s", e)

        # Queue for background DB writer (non-blocking)
        try:
            self._write_queue.put_nowait(("options", data, nifty_spot, atm_strike, ts))
        except queue.Full:
            pass

        # Notify subscribers (charts, websocket)
        for cb in self._subscribers:
            try:
                cb(data, nifty_spot)
            except Exception:
                pass

    def get_latest_options(self,limit: int = 100) -> list[dict]:
        """ Read latest options data (for charts/api) """
        with self._lock:
            return list(self._options_data)[-limit:]

    def get_spot(self) -> tuple[float | None, datetime | None]:
        """ Read latest spot price and timestamp """
        with self._lock:
            return self._spot_price, self._spot_timestamp

    def subscribe(self,callback: Callable[[list[dict],float],None]):
        """ subscribe to real-time updates (for charts/websockets) """
        self._subscribers.append(callback)

    def start_db_writer(self, db_path: str, interval: float = 5.0):
        """ Start background thread that drains queue and writes to db """
        if self._bg_thread and self._bg_thread.is_alive():
            return

        self._running = True
        self._bg_thread = threading.Thread(
            target=self._db_writer_loop,
            args=(db_path, interval),
            daemon=True
        )
        self._bg_thread.start()

    def _db_writer_loop(self, db_path: str, interval: float = 5.0):
        import sqlite3
        from nifty_algo.data.db import save_options_data_to_db, save_nifty_spot_to_db
        
        # Create connection in this thread (SQLite requires same-thread usage)
        db_conn = sqlite3.connect(db_path)
        
        try:
            while self._running:
                batch = []
                try:
                    while True:
                        batch.append(self._write_queue.get_nowait())
                except queue.Empty:
                    pass

                if batch:
                    for kind, data, spot, atm_strike, ts in batch:
                        ts_str = ts.strftime("%H:%M:%S")
                        date_str = ts.strftime("%Y-%m-%d")
                        save_options_data_to_db(db_conn, data, ts_str, date_str)
                        save_nifty_spot_to_db(db_conn, spot, atm_strike, ts_str, date_str)
                    db_conn.commit()

                time.sleep(interval)
        finally:
            db_conn.close()

    def stop_db_writer(self):
        self._running = False

# Singleton instance
_cache: MarketCache | None = None

def get_cache() -> MarketCache:
    global _cache
    if _cache is None:
        _cache = MarketCache()
        logger.info("Created new MarketCache singleton (id=%s)", id(_cache))
    return _cache

        
