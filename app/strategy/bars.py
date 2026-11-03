"""Candles and resampling of 1-minute bars into 5/15/30-minute bars.

All timestamps are naive datetimes in IST. A candle's ``ts`` is the minute it
*starts*, as returned by Groww (the 09:15 candle covers 09:15:00-09:15:59).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
TIMEFRAMES = (5, 15, 30)


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Bar:
    """A resampled bar. ``end`` is when the bar closes (and can be acted on)."""

    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    minutes: int  # how many 1m candles went into it

    @property
    def day(self) -> date:
        return self.start.date()


def session_open(day: date) -> datetime:
    return datetime.combine(day, SESSION_OPEN)


def session_close(day: date) -> datetime:
    return datetime.combine(day, SESSION_CLOSE)


def bucket_start(ts: datetime, minutes: int) -> datetime:
    """Start of the ``minutes`` bucket containing ``ts``, anchored at 09:15."""
    opened = session_open(ts.date())
    offset = int((ts - opened).total_seconds() // 60)
    return opened + timedelta(minutes=(offset // minutes) * minutes)


def in_session(ts: datetime) -> bool:
    return SESSION_OPEN <= ts.time() < SESSION_CLOSE


def resample(candles: Iterable[Candle], minutes: int) -> list[Bar]:
    """Group 1m candles into ``minutes`` bars aligned to the 09:15 open.

    Candles outside 09:15-15:30 are dropped. A bucket with missing minutes still
    becomes a bar (``minutes`` tells how many candles it had). The last bar of
    the day never closes after 15:30.
    """
    if minutes < 1:
        raise ValueError("minutes must be positive")
    bars: list[Bar] = []
    cur: list[Candle] = []
    cur_start: datetime | None = None

    def flush() -> None:
        if not cur:
            return
        end = min(
            cur_start + timedelta(minutes=minutes), session_close(cur_start.date())
        )
        bars.append(
            Bar(
                start=cur_start,
                end=end,
                open=cur[0].open,
                high=max(c.high for c in cur),
                low=min(c.low for c in cur),
                close=cur[-1].close,
                volume=sum(c.volume for c in cur),
                minutes=len(cur),
            )
        )

    for c in sorted(candles, key=lambda c: c.ts):
        if not in_session(c.ts):
            continue
        start = bucket_start(c.ts, minutes)
        if start != cur_start:
            flush()
            cur, cur_start = [], start
        if cur and cur[-1].ts == c.ts:  # duplicate minute: keep the latest
            cur[-1] = c
        else:
            cur.append(c)
    flush()
    return bars
