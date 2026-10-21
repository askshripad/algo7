"""
SQLite persistence for Breeze strangle 1-minute OHLC history.
Database: <project_root>/db/strangle_data.db
"""
from __future__ import annotations
import logging
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from nifty_algo.brokers.breeze import OptionCandle
logger = logging.getLogger(__name__)
STRANGLE_DB_FILENAME = "strangle_data.db"

def _find_project_root() -> Path:
    """Repo root (folder containing pyproject.toml or src/nifty_algo)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
        if (parent / "src" / "nifty_algo").is_dir():
            return parent
    return Path.cwd()

def get_strangle_db_path() -> Path:
    root = _find_project_root()
    db_dir = root / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / STRANGLE_DB_FILENAME

def connect_strangle_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_strangle_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_strangle_schema(conn)
    return conn

def init_strangle_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strangle_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            exchange_code TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            option_side TEXT NOT NULL,
            strike INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            volume_raw REAL,
            volume_delta REAL,
            source TEXT NOT NULL DEFAULT 'breeze',
            created_at TEXT NOT NULL,
            UNIQUE (
                symbol, exchange_code, expiry_date, trade_date,
                option_side, strike, timestamp
            )
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strangle_trade_date "
        "ON strangle_history (trade_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strangle_expiry "
        "ON strangle_history (expiry_date)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nifty_index_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            exchange_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            volume_raw REAL,
            volume_delta REAL,
            source TEXT NOT NULL DEFAULT 'breeze',
            created_at TEXT NOT NULL,
            UNIQUE (
                symbol, exchange_code, trade_date, timestamp
            )
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nifty_index_trade_date "
        "ON nifty_index_history (trade_date)"
    )
    _migrate_strangle_schema(conn)
    # nifty_daily: user-managed (e.g. CSV import: Date, Open, High, Low, Close, ATR) — do not create here.
    conn.commit()


def _migrate_strangle_schema(conn: sqlite3.Connection) -> None:
    """Backfill newly added columns for existing DB files."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(strangle_history)")
    s_cols = {row[1] for row in cur.fetchall()}
    if "volume_raw" not in s_cols:
        cur.execute("ALTER TABLE strangle_history ADD COLUMN volume_raw REAL")
    if "volume_delta" not in s_cols:
        cur.execute("ALTER TABLE strangle_history ADD COLUMN volume_delta REAL")

    cur.execute("PRAGMA table_info(nifty_index_history)")
    n_cols = {row[1] for row in cur.fetchall()}
    if "volume_raw" not in n_cols:
        cur.execute("ALTER TABLE nifty_index_history ADD COLUMN volume_raw REAL")
    if "volume_delta" not in n_cols:
        cur.execute("ALTER TABLE nifty_index_history ADD COLUMN volume_delta REAL")

def _bar_time_str(c:OptionCandle) -> str:
    # OptionCandle from `nifty_algo.brokers.breeze` uses `datetime` as the bar timestamp.
    return c.datetime.strftime("%Y-%m-%d %H:%M:%S")

def save_leg_candles(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    exchange_code: str,
    expiry_date: date,
    trade_date: date,
    option_side: str,
    strike: int,
    candles: list[OptionCandle],
    source: str = "breeze",

) -> int:
    """Upsert one leg.Return number of rows inserted/replaced."""
    if not candles:
        return 0

    side = option_side.upper()
    exp_date = expiry_date.isoformat()
    trade_date = trade_date.isoformat()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sym = _norm_symbol(symbol)
    ex = _norm_exchange(exchange_code)

    cursor = conn.cursor()
    n = 0
    prev_raw: float | None = None
    for c in candles:
        ts = _bar_time_str(c)
        raw = float(c.volume) if c.volume is not None else 0.0
        if prev_raw is None:
            delta = raw
        elif raw >= prev_raw:
            delta = raw - prev_raw
        else:
            delta = raw
        prev_raw = raw
        cursor.execute(
            """
            INSERT OR REPLACE INTO strangle_history (
                symbol, exchange_code, expiry_date, trade_date,
                option_side, strike, timestamp, open, high, low, close, volume, volume_raw, volume_delta, source, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sym, ex, exp_date, trade_date, side, strike, ts, c.open, c.high, c.low, c.close, delta, raw, delta, source, now),
        )
        n += cursor.rowcount
    conn.commit()
    return n

def save_strangle_history(
    ce_candles: list[OptionCandle],
    pe_candles: list[OptionCandle],
    *,
    expiry_date: date,
    trade_date: date,
    ce_strike: int,
    pe_strike: int,
    symbol: str = "NIFTY",
    exchange_code: str = "NFO",
    db_path: Path | None = None,
    source: str = "breeze",
) -> tuple[int, int]:
    """
    Save both legs after get_strangle_historical_1m.
    Returns (ce_rows_saved, pe_rows_saved).
    """
    conn = connect_strangle_db(db_path)
    try:
        n_ce = save_leg_candles(conn, symbol=symbol, exchange_code=exchange_code, expiry_date=expiry_date, 
        trade_date=trade_date, option_side="CE", strike=ce_strike, candles=ce_candles, source=source)
        n_pe = save_leg_candles(conn, symbol=symbol, exchange_code=exchange_code, expiry_date=expiry_date, 
        trade_date=trade_date, option_side="PE", strike=pe_strike, candles=pe_candles, source=source)
        return n_ce, n_pe
    finally:
        conn.close()

def _parse_db_timestamp(ts: str) -> datetime:
    # DB stores timestamp as "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def _norm_symbol(s: str) -> str:
    return (s or "").strip().upper()


def _norm_exchange(s: str) -> str:
    return (s or "").strip().upper()

def _load_candles_from_db(
    *,
    symbol: str,
    exchange_code: str,
    expiry_date: date,
    trade_date: date,
    option_side: str,
    strike: int,
) -> list[OptionCandle]:
    """
    Load one leg (CE/PE) from DB.
    Returns list[OptionCandle] sorted by timestamp.
    """
    from nifty_algo.brokers.breeze import OptionCandle
    conn = connect_strangle_db()
    try:
        sym = _norm_symbol(symbol)
        ex = _norm_exchange(exchange_code)
        rows = conn.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM strangle_history
            WHERE UPPER(TRIM(symbol)) = ? AND UPPER(TRIM(exchange_code)) = ?
              AND expiry_date = ? AND trade_date = ? AND option_side = ? AND strike = ?
            ORDER BY timestamp ASC
            """,
            (sym, ex, expiry_date.isoformat(), trade_date.isoformat(), option_side.upper(), strike),
        ).fetchall()

        out: list[OptionCandle] = []
        for r in rows:
            ts = r["timestamp"]
            out.append(OptionCandle(
                datetime=_parse_db_timestamp(ts),
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            ))
        return out
    finally:
        conn.close()

def get_strangle_history_from_db(
    *,
    expiry_date: date,
    trade_date: date,
    ce_strike: int,
    pe_strike: int,
    symbol: str = "NIFTY",
    exchange_code: str = "NFO"
) -> tuple[list[OptionCandle], list[OptionCandle]]:
    """
    Load CE + PE 1-minute history from DB.
    Returns (ce_candles, pe_candles).
    """
    ce = _load_candles_from_db(
        symbol=symbol,
        exchange_code=exchange_code,
        expiry_date=expiry_date,
        trade_date=trade_date,
        option_side="CE",
        strike=ce_strike,
    )
    pe = _load_candles_from_db(
        symbol=symbol,
        exchange_code=exchange_code,
        expiry_date=expiry_date,
        trade_date=trade_date,
        option_side="PE",
        strike=pe_strike,
    )
    return ce, pe

def save_nifty_index_candles(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    exchange_code: str,
    trade_date: date,
    candles: list["OptionCandle"],
    source: str = "breeze",
) -> int:
    """Save/replace 1m NIFTY cash candles for a trade_date."""
    if not candles:
        return 0

    td_s = trade_date.isoformat()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sym = _norm_symbol(symbol)
    ex = _norm_exchange(exchange_code)

    cur = conn.cursor()
    n = 0
    prev_raw: float | None = None
    for c in candles:
        ts = _bar_time_str(c)  # uses c.datetime
        raw = float(c.volume) if c.volume is not None else 0.0
        if prev_raw is None:
            delta = raw
        elif raw >= prev_raw:
            delta = raw - prev_raw
        else:
            delta = raw
        prev_raw = raw
        cur.execute(
            """
            INSERT OR REPLACE INTO nifty_index_history (
                symbol, exchange_code, trade_date, timestamp,
                open, high, low, close, volume, volume_raw, volume_delta,
                source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sym,
                ex,
                td_s,
                ts,
                c.open,
                c.high,
                c.low,
                c.close,
                delta,
                raw,
                delta,
                source,
                now,
            ),
        )
        n += cur.rowcount
    conn.commit()
    return n


def save_nifty_index_historical_1m(
    candles: list["OptionCandle"],
    *,
    symbol: str = "NIFTY",
    exchange_code: str = "NSE",
    trade_date: date,
    db_path: Path | None = None,
    source: str = "breeze",
) -> int:
    """Convenience wrapper: open DB, save candles, close DB."""
    conn = connect_strangle_db(db_path)
    try:
        return save_nifty_index_candles(
            conn,
            symbol=symbol,
            exchange_code=exchange_code,
            trade_date=trade_date,
            candles=candles,
            source=source,
        )
    finally:
        conn.close()


def get_nifty_index_history_from_db(
    *,
    symbol: str = "NIFTY",
    exchange_code: str = "NSE",
    trade_date: date,
) -> list["OptionCandle"]:
    """Load NIFTY index 1m history from DB."""
    from nifty_algo.brokers.breeze import OptionCandle

    sym = _norm_symbol(symbol)
    ex = _norm_exchange(exchange_code)
    conn = connect_strangle_db()
    try:
        rows = conn.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM nifty_index_history
            WHERE UPPER(TRIM(symbol)) = ?
              AND UPPER(TRIM(exchange_code)) = ?
              AND trade_date = ?
            ORDER BY timestamp ASC
            """,
            (sym, ex, trade_date.isoformat()),
        ).fetchall()

        out: list[OptionCandle] = []
        for r in rows:
            ts = r["timestamp"]
            out.append(
                OptionCandle(
                    datetime=_parse_db_timestamp(ts),
                    open=float(r["open"]) if r["open"] is not None else 0.0,
                    high=float(r["high"]) if r["high"] is not None else 0.0,
                    low=float(r["low"]) if r["low"] is not None else 0.0,
                    close=float(r["close"]) if r["close"] is not None else 0.0,
                    volume=float(r["volume"]) if r["volume"] is not None else 0.0,
                )
            )
        return out
    finally:
        conn.close()


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if s == "":
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _normalize_nifty_daily_date_string(s: str) -> str:
    """
    Pad day to 2 digits for ``d-mmm-yy`` / ``dd-mmm-yy`` (e.g. ``1-Dec-25`` → ``01-Dec-25``).

    User CSVs often omit the leading zero on the day. On Windows, ``strptime`` with ``%d``
    can reject single-digit days; normalizing avoids that.
    """
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{2})$", s.strip())
    if m:
        d, mon, y = m.groups()
        try:
            day = int(d)
            if 1 <= day <= 31:
                return f"{day:02d}-{mon}-{y}"
        except ValueError:
            pass
    return s


def _parse_nifty_daily_date(value: Any) -> date | None:
    """
    Parse a cell from ``nifty_daily.Date`` (CSV may use ISO, ``1-Dec-25``, ``20-Oct-25``, etc.).
    SQLite string compare / ORDER BY is wrong for mixed formats — callers must use real dates.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = _normalize_nifty_daily_date_string(s)

    fmts = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%b-%y",
        "%d-%b-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%d-%m-%y",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Strip time if present
    if " " in s:
        try:
            return _parse_nifty_daily_date(s.split()[0])
        except Exception:
            pass
    logger.debug("Could not parse nifty_daily date: %r", s)
    return None


def _nifty_daily_column_map(
    conn: sqlite3.Connection,
) -> tuple[str, str, str, str | None, str | None] | None:
    """
    Map logical date / close / atr / optional high & low to actual ``nifty_daily`` column names.

    Supports CSV-style headers (case-insensitive), e.g. ``Date``, ``Close``, ``ATR``, ``High``, ``Low``.
    """
    try:
        info = conn.execute("PRAGMA table_info(nifty_daily)").fetchall()
    except sqlite3.OperationalError:
        return None
    if not info:
        return None
    names = [str(r[1]) for r in info]
    lower = {n.lower(): n for n in names}

    # Date column (CSV: "Date")
    date_col = lower.get("date") or lower.get("trade_date") or lower.get("dt")
    # Close column (CSV: "Close")
    close_col = lower.get("close") or lower.get("nifty_close") or lower.get("adj_close")
    # ATR column (CSV: "ATR")
    atr_col = lower.get("atr") or lower.get("atr14") or lower.get("atr_value")
    # Optional OHLC (for daily range on trade date)
    high_col = lower.get("high") or lower.get("nifty_high")
    low_col = lower.get("low") or lower.get("nifty_low")

    if not date_col or not close_col or not atr_col:
        logger.warning(
            "nifty_daily: need Date (or date/trade_date), Close (or close), ATR (or atr); found: %s",
            names,
        )
        return None
    return date_col, close_col, atr_col, high_col, low_col


def get_nifty_daily_row_before(trade_date: date) -> dict[str, Any] | None:
    """
    Latest **calendar** row in ``nifty_daily`` strictly before ``trade_date``.

    Used for **previous day's Close and ATR** (e.g. Nifty open = prior close). This is **not**
    the same row as :func:`get_nifty_daily_row_for_trade_date`, which targets the trade day
    for High/Low.

    Does not rely on SQLite string ordering (CSV dates like ``20-Oct-25`` vs ``2025-12-04``).
    """
    conn = connect_strangle_db()
    try:
        mapped = _nifty_daily_column_map(conn)
        if not mapped:
            return None
        date_col, close_col, atr_col, _high_c, _low_c = mapped
        q = f'SELECT "{date_col}" AS d, "{close_col}" AS c, "{atr_col}" AS a FROM nifty_daily'
        rows = conn.execute(q).fetchall()
        best_d: date | None = None
        best_row = None
        for row in rows:
            parsed = _parse_nifty_daily_date(row["d"])
            if parsed is None:
                continue
            if parsed >= trade_date:
                continue
            if best_d is None or parsed > best_d:
                best_d = parsed
                best_row = row
        if best_row is None or best_d is None:
            return None
        return {
            "date": best_d.isoformat(),
            "close": _safe_float(best_row["c"]),
            "atr": _safe_float(best_row["a"]),
        }
    except sqlite3.OperationalError as e:
        err = str(e).lower()
        if "no such table" in err or "no such column" in err:
            logger.warning("nifty_daily query skipped: %s", e)
            return None
        logger.exception("nifty_daily OperationalError: %s", e)
        return None
    except (sqlite3.DatabaseError, KeyError, TypeError) as e:
        logger.warning("nifty_daily read failed: %s", e)
        return None
    finally:
        conn.close()


def get_nifty_spot_open_for_trade_date(trade_date: date) -> tuple[float | None, str | None]:
    """
    Cash **open** for the given session day (for ATM strike selection).

    1. ``nifty_daily`` row matching ``trade_date`` — **Open** column if present.
    2. Else first 1m bar **open** in ``nifty_index_history`` for that ``trade_date``.

    Returns ``(value, source_hint)`` or ``(None, None)`` if unavailable.
    """
    conn = connect_strangle_db()
    try:
        try:
            info = conn.execute("PRAGMA table_info(nifty_daily)").fetchall()
        except sqlite3.OperationalError:
            info = []
        names = [str(r[1]) for r in info]
        lower = {n.lower(): n for n in names}
        date_col = lower.get("date") or lower.get("trade_date") or lower.get("dt")
        open_col = lower.get("open") or lower.get("nifty_open")
        if date_col and open_col:
            q = f'SELECT "{date_col}" AS d, "{open_col}" AS o FROM nifty_daily'
            rows = conn.execute(q).fetchall()
            for row in rows:
                parsed = _parse_nifty_daily_date(row["d"])
                if parsed == trade_date:
                    o = _safe_float(row["o"])
                    if o is not None and o == o and o > 0:
                        return float(o), "nifty_daily"
                    break
    except (sqlite3.DatabaseError, KeyError, TypeError) as e:
        logger.debug("get_nifty_spot_open nifty_daily: %s", e)
    finally:
        conn.close()

    try:
        candles = get_nifty_index_history_from_db(trade_date=trade_date)
        if candles:
            o = float(candles[0].open)
            if o > 0 and o == o:
                return o, "nifty_index_history"
    except Exception as e:
        logger.debug("get_nifty_spot_open index 1m: %s", e)
    return None, None


def get_nifty_daily_row_for_trade_date(trade_date: date) -> dict[str, Any] | None:
    """
    Row in ``nifty_daily`` whose parsed date equals the **selected trade date** (same calendar day in DB).

    Returns ``high``, ``low``, and ``range`` (``high - low``) from the **High** / **Low** columns.
    Contrast: :func:`get_nifty_daily_row_before` is used for **prior day's** Close/ATR, not this row.

    If the table has no High/Low columns, returns ``None``. Does not rely on SQLite string
    equality for dates (CSV may use ``1-Dec-25`` etc.).
    """
    conn = connect_strangle_db()
    try:
        mapped = _nifty_daily_column_map(conn)
        if not mapped:
            return None
        date_col, _close_col, _atr_col, high_col, low_col = mapped
        if not high_col or not low_col:
            logger.debug(
                "nifty_daily: High/Low columns missing; cannot return daily range for %s",
                trade_date,
            )
            return None
        q = f'SELECT "{date_col}" AS d, "{high_col}" AS h, "{low_col}" AS lo FROM nifty_daily'
        rows = conn.execute(q).fetchall()
        for row in rows:
            parsed = _parse_nifty_daily_date(row["d"])
            if parsed is None:
                continue
            if parsed != trade_date:
                continue
            hi = _safe_float(row["h"])
            lo = _safe_float(row["lo"])
            rng: float | None = None
            if hi is not None and lo is not None:
                rng = float(hi) - float(lo)
            return {
                "date": trade_date.isoformat(),
                "high": hi,
                "low": lo,
                "range": rng,
            }
        return None
    except sqlite3.OperationalError as e:
        err = str(e).lower()
        if "no such table" in err or "no such column" in err:
            logger.warning("nifty_daily trade-date row skipped: %s", e)
            return None
        logger.exception("nifty_daily OperationalError: %s", e)
        return None
    except (sqlite3.DatabaseError, KeyError, TypeError) as e:
        logger.warning("nifty_daily trade-date read failed: %s", e)
        return None
    finally:
        conn.close()


