"""Read-only access to Groww market data, with rate limiting and retries.

Only the data methods below are reachable through :class:`ReadOnlyGroww`.
Order placement is impossible by construction: any other attribute raises.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import date, datetime, timedelta

from .strategy.contracts import parse_option_symbol

SPOT_SYMBOL = "NSE-NIFTY"
UNDERLYING = "NIFTY"
EXCHANGE = "NSE"
SEG_CASH = "CASH"
SEG_FNO = "FNO"
MAX_1M_DAYS = 30  # Groww limit for 1-minute candles per request

ALLOWED_METHODS = frozenset({"get_historical_candles", "get_expiries", "get_contracts"})


class OrderBlockedError(RuntimeError):
    pass


class ReadOnlyGroww:
    """Wraps a GrowwAPI client and exposes only historical-data calls."""

    def __init__(
        self,
        client,
        min_interval: float = 0.25,
        retries: int = 4,
        sleep: Callable[[float], None] = _time.sleep,
    ):
        self._client = client
        self._min_interval = min_interval
        self._retries = retries
        self._sleep = sleep
        self._last = 0.0

    def __getattr__(self, name):
        if name in ALLOWED_METHODS:
            return lambda *a, **kw: self._call(name, *a, **kw)
        raise OrderBlockedError(
            f"'{name}' is not allowed. This app only reads market data; it never "
            "places, modifies or cancels orders."
        )

    def _call(self, name, *args, **kwargs):
        method = getattr(self._client, name)
        kwargs.setdefault("timeout", 30)
        delay = 1.0
        for attempt in range(self._retries + 1):
            wait = self._min_interval - (_time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = _time.monotonic()
            try:
                return method(*args, **kwargs)
            except Exception as exc:  # network blip / rate limit: back off and retry
                if attempt == self._retries or _is_permanent(exc):
                    raise
                self._sleep(delay)
                delay *= 2


def _is_permanent(exc: Exception) -> bool:
    code = str(getattr(exc, "code", "") or "")
    msg = str(exc).lower()
    if code in {"400", "401", "403", "404"} or "not found" in msg or "invalid" in msg:
        return True
    return False


def parse_ts(value) -> datetime:
    """Groww returns 'YYYY-MM-DDTHH:MM:SS', 'YYYY-MM-DD HH:MM:SS' or epoch secs."""
    if isinstance(value, int | float):
        from .groww_client import IST

        return datetime.fromtimestamp(value, IST).replace(tzinfo=None)
    s = str(value).replace("T", " ")
    if "+" in s[10:]:
        s = s[: s.rindex("+")]
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def parse_candles(resp: dict) -> list[tuple]:
    out = []
    for c in (resp or {}).get("candles") or []:
        if c is None or len(c) < 5 or any(v is None for v in c[1:5]):
            continue
        out.append(
            (
                parse_ts(c[0]),
                float(c[1]),
                float(c[2]),
                float(c[3]),
                float(c[4]),
                float(c[5]) if len(c) > 5 and c[5] is not None else 0.0,
                float(c[6]) if len(c) > 6 and c[6] is not None else None,
            )
        )
    return out


def fetch_candles(
    api: ReadOnlyGroww, symbol: str, segment: str, start: datetime, end: datetime
) -> list[tuple]:
    return parse_candles(
        api.get_historical_candles(
            exchange=EXCHANGE,
            segment=segment,
            groww_symbol=symbol,
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
            candle_interval="1minute",
        )
    )


def day_chunks(start: date, end: date, size: int = MAX_1M_DAYS):
    """Split [start, end] into windows of at most ``size`` calendar days."""
    cur = start
    while cur <= end:
        last = min(cur + timedelta(days=size - 1), end)
        yield cur, last
        cur = last + timedelta(days=1)


def fetch_expiries(api: ReadOnlyGroww, year: int, month: int) -> list[date]:
    resp = api.get_expiries(
        exchange=EXCHANGE, underlying_symbol=UNDERLYING, year=year, month=month
    )
    out = []
    for s in (resp or {}).get("expiries") or []:
        try:
            out.append(datetime.strptime(str(s)[:10], "%Y-%m-%d").date())
        except ValueError:
            continue
    return sorted(set(out))


def fetch_contracts(api: ReadOnlyGroww, expiry: date) -> list[dict]:
    resp = api.get_contracts(
        exchange=EXCHANGE, underlying_symbol=UNDERLYING, expiry_date=expiry.isoformat()
    )
    rows = []
    for sym in (resp or {}).get("contracts") or []:
        parsed = parse_option_symbol(str(sym))
        if parsed is None:
            continue
        exp, strike, opt_type = parsed
        rows.append(
            dict(
                symbol=str(sym),
                underlying=UNDERLYING,
                expiry=expiry,
                strike=strike,
                opt_type=opt_type,
            )
        )
    return rows
