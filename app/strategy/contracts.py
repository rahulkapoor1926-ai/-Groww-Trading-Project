"""Strike, option type and expiry selection for each execution mode."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .indicators import BULLISH

STRIKE_STEP = 50
ITM_STEPS = 5

ITM5_BUY = "ITM5_BUY"
ATM_BUY = "ATM_BUY"
ATM_SELL = "ATM_SELL"
MODES = (ITM5_BUY, ATM_BUY, ATM_SELL)
MODE_LABELS = {
    ITM5_BUY: "Deep ITM buy (ITM 5)",
    ATM_BUY: "ATM buy",
    ATM_SELL: "ATM sell",
}

BUY = "BUY"
SELL = "SELL"

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def atm_strike(spot: float, step: int = STRIKE_STEP) -> int:
    """Nearest strike to ``spot``; an exact half-way price rounds up."""
    return int(math.floor(spot / step + 0.5) * step)


@dataclass(frozen=True)
class Leg:
    opt_type: str  # "CE" or "PE"
    strike: int
    side: str  # BUY or SELL (the entry order)


def pick_leg(mode: str, direction: int, spot: float) -> Leg:
    atm = atm_strike(spot)
    bull = direction == BULLISH
    if mode == ITM5_BUY:
        shift = ITM_STEPS * STRIKE_STEP
        return Leg("CE", atm - shift, BUY) if bull else Leg("PE", atm + shift, BUY)
    if mode == ATM_BUY:
        return Leg("CE" if bull else "PE", atm, BUY)
    if mode == ATM_SELL:
        return Leg("PE" if bull else "CE", atm, SELL)
    raise ValueError(f"unknown mode {mode!r}")


def nearest_expiry(day: date, expiries: Sequence[date]) -> date | None:
    """First expiry on or after ``day`` (``expiries`` must be sorted).

    On expiry day itself this returns that same day (0 DTE)."""
    i = bisect_left(expiries, day)
    return expiries[i] if i < len(expiries) else None


def option_symbol(expiry: date, strike: int | float, opt_type: str) -> str:
    """Groww symbol, e.g. NSE-NIFTY-30Sep25-24650-CE."""
    k = int(strike) if float(strike).is_integer() else strike
    exp = f"{expiry.day:02d}{_MONTHS[expiry.month - 1]}{expiry:%y}"
    return f"NSE-NIFTY-{exp}-{k}-{opt_type}"


def parse_option_symbol(symbol: str) -> tuple[date, float, str] | None:
    """Inverse of :func:`option_symbol`. Returns (expiry, strike, type) or None."""
    parts = symbol.split("-")
    if len(parts) != 5 or parts[4] not in ("CE", "PE"):
        return None
    raw = parts[2]
    try:
        day, mon, yy = int(raw[:2]), _MONTHS.index(raw[2:5]), int(raw[5:])
        return date(2000 + yy, mon + 1, day), float(parts[3]), parts[4]
    except (ValueError, IndexError):
        return None
