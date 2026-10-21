"""
Fyers NSE Nifty option symbology (see Fyers API appendix / community examples).

Weeklies:
  NSE:NIFTY + YY + <month code> + DD + Strike + CE|PE
  Month code: Jan–Sep = 1–9 (no pad); Oct/Nov/Dec = O / N / D
  Example: 19 Oct 2026 weekly → NSE:NIFTY26O19…

Monthlies:
  NSE:NIFTY + YY + MMM + Strike + CE|PE  (no calendar day)
  Example: Oct 2026 monthly (27 Oct this cycle) → NSE:NIFTY26OCT…

``FYERS_OPTION_SYMBOL_STYLE``:
  - unpadded / auto / compact — **auto weekly vs monthly** (default)
  - weekly — force weekly codes even on month-end dates
  - monthly / mar — force YYMMM (no day)
  - padded — YYMMDD (often invalid for Oct–Dec weeklies)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from nifty_algo.config import config

logger = logging.getLogger(__name__)

_MON3 = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

# Fyers weekly month codes for Oct/Nov/Dec (avoid clash with monthly OCT/NOV/DEC).
_WEEKLY_MONTH_LETTER = {10: "O", 11: "N", 12: "D"}


def last_thursday_of_month(year: int, month: int) -> date:
    """NSE monthly Nifty expiry is normally the last Thursday of the month."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != 3:  # Thursday
        d -= timedelta(days=1)
    return d


def is_likely_nifty_monthly_expiry(expiry_date: date) -> bool:
    """
    True if ``expiry_date`` looks like the monthly contract for that month.

    Matches last Thursday, or up to 3 calendar days earlier (holiday / special
    shifts — e.g. Oct 2026 monthly on 27 when last Thursday is 29).
    """
    lt = last_thursday_of_month(expiry_date.year, expiry_date.month)
    if expiry_date > lt:
        return False
    return (lt - expiry_date).days <= 3


def _normalize_style(raw: str | None) -> str:
    s = (raw or "unpadded").strip().lower()
    aliases = {
        "auto": "auto",
        "compact": "auto",
        "numeric": "auto",
        "default": "auto",
        "unpadded": "auto",
        "yymmdd": "padded",
        "letter": "monthly",
        "mmm": "monthly",
        "mar": "monthly",
    }
    s = aliases.get(s, s)
    if s not in ("auto", "weekly", "monthly", "padded"):
        return "auto"
    return s


def _weekly_segment(expiry_date: date) -> str:
    yy = expiry_date.strftime("%y")
    dd = expiry_date.strftime("%d")
    mcode = _WEEKLY_MONTH_LETTER.get(expiry_date.month, str(expiry_date.month))
    return f"{yy}{mcode}{dd}"


def _monthly_segment(expiry_date: date) -> str:
    return f"{expiry_date.strftime('%y')}{_MON3[expiry_date.month - 1]}"


def build_fyers_option_symbol(
    expiry_date: date,
    strike: int,
    option_type: str,
    *,
    force_monthly: bool | None = None,
) -> str:
    """
    Full Fyers option symbol for NSE Nifty (NSE:NIFTY…).

    ``force_monthly``: True/False overrides style auto-detect; None uses style + date heuristic.
    """
    opt = (option_type or "CE").upper()
    if opt not in ("CE", "PE"):
        opt = "CE"
    strike_i = int(strike)

    style = _normalize_style(getattr(config, "FYERS_OPTION_SYMBOL_STYLE", None))

    if force_monthly is True:
        use_monthly = True
    elif force_monthly is False:
        use_monthly = False
    elif style == "monthly":
        use_monthly = True
    elif style == "weekly":
        use_monthly = False
    elif style == "padded":
        use_monthly = False
    else:
        # auto / unpadded: pick monthly ticker near month-end Thursday (incl. holiday shift)
        use_monthly = is_likely_nifty_monthly_expiry(expiry_date)

    if style == "padded" and force_monthly is not True:
        seg = expiry_date.strftime("%y%m%d")
        kind = "padded"
    elif use_monthly:
        seg = _monthly_segment(expiry_date)
        kind = "monthly"
    else:
        seg = _weekly_segment(expiry_date)
        kind = "weekly"

    sym = f"NSE:NIFTY{seg}{strike_i}{opt}"
    logger.debug(
        "Fyers option symbol (style=%s kind=%s expiry=%s): %s",
        style,
        kind,
        expiry_date.isoformat(),
        sym,
    )
    return sym


def build_fyers_option_symbol_candidates(
    expiry_date: date,
    strike: int,
    option_type: str,
) -> list[str]:
    """
    Primary symbol plus the alternate weekly/monthly form (deduped).

    Useful when auto-detect is wrong or the broker rejects the first ticker.
    """
    primary = build_fyers_option_symbol(expiry_date, strike, option_type)
    alt_monthly = build_fyers_option_symbol(
        expiry_date, strike, option_type, force_monthly=True
    )
    alt_weekly = build_fyers_option_symbol(
        expiry_date, strike, option_type, force_monthly=False
    )
    out: list[str] = []
    for s in (primary, alt_monthly, alt_weekly):
        if s not in out:
            out.append(s)
    return out
