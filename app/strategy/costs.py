"""Slippage and the Indian index-options cost stack.

Every rate lives in ``CHARGE_SCHEDULE`` with the date it took effect, so a
backtest over old dates uses the rates that applied then. Edit that list if a
rate changes.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

TICK = 0.05


def _ticks(price: float) -> float:
    # round() first so 100.05 / 0.05 doesn't become 2000.9999999
    return round(price / TICK, 6)


def round_up_tick(price: float) -> float:
    return round(math.ceil(_ticks(price)) * TICK, 2)


def round_down_tick(price: float) -> float:
    return round(math.floor(_ticks(price)) * TICK, 2)


@dataclass(frozen=True)
class Slippage:
    pct: float = 0.005  # 0.5% of premium
    points: float = 0.5  # plus fixed points

    def buy(self, price: float) -> float:
        """You pay more: rounded *up* to the tick."""
        return round_up_tick(price * (1 + self.pct) + self.points)

    def sell(self, price: float) -> float:
        """You receive less: rounded *down* to the tick, never below 0.05."""
        return max(TICK, round_down_tick(price * (1 - self.pct) - self.points))


@dataclass(frozen=True)
class ChargeRates:
    effective_from: date
    brokerage_per_order: float  # Groww F&O: flat Rs 20 per executed order
    stt_sell: float  # on sell-side premium
    exchange: float  # NSE transaction charge, on premium (both sides)
    sebi: float  # Rs 10 per crore = 1e-6, on premium (both sides)
    stamp_buy: float  # on buy-side premium
    gst: float  # on brokerage + exchange + SEBI


CHARGE_SCHEDULE: tuple[ChargeRates, ...] = (
    ChargeRates(date(2000, 1, 1), 20.0, 0.000625, 0.0005, 1e-6, 0.00003, 0.18),
    # NSE "true-to-label" transaction charge, and STT 0.1% (Budget 2024)
    ChargeRates(date(2024, 10, 1), 20.0, 0.001, 0.0003503, 1e-6, 0.00003, 0.18),
    # Budget 2026: STT on options premium 0.1% -> 0.15%
    ChargeRates(date(2026, 4, 1), 20.0, 0.0015, 0.0003503, 1e-6, 0.00003, 0.18),
)


def rates_on(day: date) -> ChargeRates:
    current = CHARGE_SCHEDULE[0]
    for r in CHARGE_SCHEDULE:
        if r.effective_from <= day:
            current = r
    return current


@dataclass(frozen=True)
class Charges:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage
            + self.stt
            + self.exchange
            + self.sebi
            + self.stamp
            + self.gst,
            2,
        )

    def as_dict(self) -> dict:
        return {**asdict(self), "total": self.total}


def round_trip_charges(buy_value: float, sell_value: float, day: date) -> Charges:
    """Charges for one buy order and one sell order (in rupees of premium)."""
    r = rates_on(day)
    turnover = buy_value + sell_value
    brokerage = 2 * r.brokerage_per_order
    exchange = round(turnover * r.exchange, 2)
    sebi = round(turnover * r.sebi, 2)
    return Charges(
        brokerage=round(brokerage, 2),
        stt=round(sell_value * r.stt_sell, 2),
        exchange=exchange,
        sebi=sebi,
        stamp=round(buy_value * r.stamp_buy, 2),
        gst=round((brokerage + exchange + sebi) * r.gst, 2),
    )
