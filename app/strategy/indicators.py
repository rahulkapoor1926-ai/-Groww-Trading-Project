"""EMA and 9/21 crossover detection on closed bars."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from .bars import Bar

BULLISH = 1
BEARISH = -1


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Standard EMA with alpha = 2/(period+1), seeded by the SMA of the first
    ``period`` values. Entries before the seed are ``None``."""
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


@dataclass(frozen=True)
class Signal:
    """A crossover detected on the close of ``bars[bar_index]``."""

    bar_index: int
    time: datetime  # bar close time = earliest moment we can act
    direction: int  # BULLISH or BEARISH
    spot: float  # close of the signal bar
    ema_fast: float
    ema_slow: float

    @property
    def day(self) -> date:
        return self.time.date()


def crossovers(bars: Sequence[Bar], fast: int = 9, slow: int = 21) -> list[Signal]:
    """Detect EMA crossovers.

    The spread ``fast - slow`` is rounded to 0.01. A bar whose spread rounds to
    exactly 0 is a tie: it is never a cross by itself. A cross happens on the
    first bar whose spread sign differs from the last *non-zero* sign. The first
    non-zero sign only initialises the state (no cross on warm-up).
    """
    closes = [b.close for b in bars]
    ef, es = ema(closes, fast), ema(closes, slow)
    signals: list[Signal] = []
    last_sign = 0
    for i, bar in enumerate(bars):
        if ef[i] is None or es[i] is None:
            continue
        spread = round(ef[i] - es[i], 2)
        sign = (spread > 0) - (spread < 0)
        if sign == 0:
            continue
        if last_sign != 0 and sign != last_sign:
            signals.append(
                Signal(
                    bar_index=i,
                    time=bar.end,
                    direction=sign,
                    spot=bar.close,
                    ema_fast=ef[i],
                    ema_slow=es[i],
                )
            )
        last_sign = sign
    return signals
