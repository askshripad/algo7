from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import logging

from nifty_algo.config import config

logger = logging.getLogger(__name__)

_last_snapshot_time: float | None = None


def setup_database(cfg=config):
    if not cfg.USE_DATABASE:
        return None

    log_dir = Path(cfg.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / cfg.DATABASE_NAME

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS options_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            strike REAL,
            option_type TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            ltp REAL,
            volume REAL,
            prev_close REAL,
            delta REAL,
            gamma REAL,
            theta REAL,
            vega REAL,
            iv REAL,
            timestamp TEXT,
            date TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS nifty_spot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nifty_spot REAL,
            atm_strike REAL,
            timestamp TEXT,
            date TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS volume_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            atm_strike REAL,
            otm_call_volume REAL,
            otm_put_volume REAL,
            condition_met INTEGER,
            condition_type TEXT,
            timestamp TEXT,
            date TEXT
        )
        """
    )

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_options_date ON options_data(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_options_symbol ON options_data(symbol)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_nifty_date ON nifty_spot(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_volume_date ON volume_analysis(date)")

    _migrate_database_schema(cursor)

    conn.commit()
    return conn


def _migrate_database_schema(cursor: sqlite3.Cursor):
    cursor.execute("PRAGMA table_info(options_data)")
    existing = {row[1] for row in cursor.fetchall()}
    columns = {
        "symbol": "TEXT",
        "strike": "REAL",
        "option_type": "TEXT",
        "open": "REAL",
        "high": "REAL",
        "low": "REAL",
        "close": "REAL",
        "ltp": "REAL",
        "volume": "REAL",
        "prev_close": "REAL",
        "delta": "REAL",
        "gamma": "REAL",
        "theta": "REAL",
        "vega": "REAL",
        "iv": "REAL",
        "timestamp": "TEXT",
        "date": "TEXT",
    }

    for col, col_type in columns.items():
        if col not in existing:
            cursor.execute(f"ALTER TABLE options_data ADD COLUMN {col} {col_type}")

    cursor.execute("PRAGMA table_info(nifty_spot)")
    existing_spot = {row[1] for row in cursor.fetchall()}
    spot_columns = {
        "nifty_spot": "REAL",
        "atm_strike": "REAL",
        "timestamp": "TEXT",
        "date": "TEXT",
    }

    for col, col_type in spot_columns.items():
        if col not in existing_spot:
            cursor.execute(f"ALTER TABLE nifty_spot ADD COLUMN {col} {col_type}")

    if "spot_price" not in existing_spot:
        cursor.execute("ALTER TABLE nifty_spot ADD COLUMN spot_price REAL")


def save_options_data_to_db(conn, all_option_data, timestamp_str: str, date_str: str):
    if not conn:
        return

    cursor = conn.cursor()
    for row in all_option_data:
        cursor.execute(
            """
            INSERT INTO options_data (
                symbol, strike, option_type,
                open, high, low, close, ltp, volume, prev_close,
                delta, gamma, theta, vega, iv,
                timestamp, date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("symbol") or row.get("Symbol"),
                row.get("strike"),
                row.get("option_type"),
                row.get("Open"),
                row.get("High"),
                row.get("Low"),
                row.get("Close"),
                row.get("LTP"),
                row.get("Volume"),
                row.get("Prev_Close"),
                row.get("Delta"),
                row.get("Gamma"),
                row.get("Theta"),
                row.get("Vega"),
                row.get("IV"),
                timestamp_str,
                date_str,
            ),
        )
    conn.commit()


def _get_nifty_spot_columns(cursor: sqlite3.Cursor) -> set[str]:
    cursor.execute("PRAGMA table_info(nifty_spot)")
    return {row[1] for row in cursor.fetchall()}


def save_nifty_spot_to_db(conn, nifty_spot: float, atm_strike: float, timestamp_str: str, date_str: str):
    if not conn:
        return
    cursor = conn.cursor()
    columns = _get_nifty_spot_columns(cursor)
    if "spot_price" in columns and "nifty_spot" in columns:
        cursor.execute(
            """
            INSERT INTO nifty_spot (nifty_spot, spot_price, atm_strike, timestamp, date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (nifty_spot, nifty_spot, atm_strike, timestamp_str, date_str),
        )
    elif "spot_price" in columns:
        cursor.execute(
            """
            INSERT INTO nifty_spot (spot_price, atm_strike, timestamp, date)
            VALUES (?, ?, ?, ?)
            """,
            (nifty_spot, atm_strike, timestamp_str, date_str),
        )
    else:
        cursor.execute(
            """
            INSERT INTO nifty_spot (nifty_spot, atm_strike, timestamp, date)
            VALUES (?, ?, ?, ?)
            """,
            (nifty_spot, atm_strike, timestamp_str, date_str),
        )
    conn.commit()


def should_take_snapshot() -> bool:
    global _last_snapshot_time
    now = time.time()

    if _last_snapshot_time is None:
        _last_snapshot_time = now
        return True

    if now - _last_snapshot_time >= config.DATA_SNAPSHOT_INTERVAL:
        _last_snapshot_time = now
        return True

    return False


def get_nifty_close_price(conn, date_str: str) -> float | None:
    if not conn:
        return None
    cursor = conn.cursor()
    columns = _get_nifty_spot_columns(cursor)
    if "spot_price" in columns and "nifty_spot" in columns:
        select_column = "COALESCE(nifty_spot, spot_price)"
    elif "spot_price" in columns:
        select_column = "spot_price"
    else:
        select_column = "nifty_spot"
    cursor.execute(
        f"""
        SELECT {select_column}
        FROM nifty_spot
        WHERE date = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (date_str,),
    )
    row = cursor.fetchone()
    return float(row[0]) if row else None


def log_end_of_day_summary(conn, date_str: str):
    if not conn:
        return

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM options_data WHERE date = ?", (date_str,))
    options_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM nifty_spot WHERE date = ?", (date_str,))
    spot_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM volume_analysis WHERE date = ?", (date_str,))
    volume_count = cursor.fetchone()[0]

    close_price = get_nifty_close_price(conn, date_str)

    logger.info("=== End of Day Summary ===")
    logger.info("Date: %s", date_str)
    logger.info("Options rows: %s", options_count)
    logger.info("Nifty spot rows: %s", spot_count)
    logger.info("Volume rows: %s", volume_count)
    logger.info("Nifty close (last spot): %s", close_price)
