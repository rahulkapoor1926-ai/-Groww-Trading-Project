"""A deterministic fake of the Groww historical-data API."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from app.strategy.contracts import option_symbol, parse_option_symbol

HOLIDAYS = {date(2025, 10, 2)}


def trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def spot_price(ts: datetime) -> float:
    k = (ts - datetime(2025, 1, 1)).total_seconds() / 60
    return round(
        24500 + 180 * math.sin(k / 41) + 60 * math.sin(k / 7.3) + 0.01 * (k % 97), 2
    )


def option_price(sym: str, ts: datetime) -> float:
    exp, strike, typ = parse_option_symbol(sym)
    s = spot_price(ts)
    intrinsic = max(s - strike, 0) if typ == "CE" else max(strike - s, 0)
    dte = max((exp - ts.date()).days, 0)
    return round(intrinsic + 20 + 8 * dte + 0.03 * (ts.minute % 13), 2)


def tuesdays(year: int, month: int) -> list[date]:
    d = date(year, month, 1)
    out = []
    while d.month == month:
        if d.weekday() == 1:
            out.append(d)
        d += timedelta(days=1)
    return out


class FakeGroww:
    def __init__(self, now=None, missing_every: int = 0):
        self.calls: list[tuple] = []
        self.now = now  # callable returning "current" time, or None = no limit
        self.missing_every = missing_every

    def _range(self, start, end):
        t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        e = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        if self.now is not None:
            e = min(e, self.now())
        while t < e:
            if trading_day(t.date()) and (9, 15) <= (t.hour, t.minute) < (15, 30):
                yield t
            t += timedelta(minutes=1)

    def get_historical_candles(
        self,
        exchange,
        segment,
        groww_symbol,
        start_time,
        end_time,
        candle_interval,
        timeout=None,
    ):
        self.calls.append(("candles", groww_symbol, start_time, end_time))
        assert candle_interval == "1minute"
        out = []
        for i, t in enumerate(self._range(start_time, end_time)):
            if groww_symbol == "NSE-NIFTY":
                p = spot_price(t)
                out.append([t.isoformat(), p, p + 2, p - 2, p + 0.5, 0, None])
            else:
                if self.missing_every and i % self.missing_every == 3:
                    continue
                p = option_price(groww_symbol, t)
                out.append(
                    [t.strftime("%Y-%m-%d %H:%M:%S"), p, p + 1, p - 1, p, 100, 5000]
                )
        return {"candles": out}

    def get_expiries(
        self, exchange, underlying_symbol, year=None, month=None, timeout=None
    ):
        self.calls.append(("expiries", year, month))
        return {"expiries": [d.isoformat() for d in tuesdays(year, month)]}

    def get_contracts(self, exchange, underlying_symbol, expiry_date, timeout=None):
        self.calls.append(("contracts", expiry_date))
        exp = date.fromisoformat(expiry_date)
        return {
            "contracts": [
                option_symbol(exp, k, t)
                for k in range(22000, 27001, 50)
                for t in ("CE", "PE")
            ]
        }

    def place_order(self, *a, **kw):  # pragma: no cover - must never be reached
        raise AssertionError("place_order must never be called")
