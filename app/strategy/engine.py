"""The 9/21 EMA crossover trade state machine.

The same :class:`Strategy` is driven bar by bar by the backtester and by the
paper engine. Rules (see the plan for the reasoning):

* A signal at bar close time T is filled on the option's 1m candle that opens
  at T, at its open price. If that candle is missing, the close of the latest
  candle in the previous ``fill_fallback_minutes`` minutes is used; otherwise
  the entry is skipped and logged.
* Signals are allowed to open trades only if start <= T <= cutoff (inclusive).
* An open trade exits on the opposite cross (any time before 15:00) or at the
  15:00 square-off (open of the 15:00 option candle).
* An opposite cross inside the window closes and opens the reverse trade at the
  same minute, with a fresh strike. After the cutoff it only closes.
* One position at a time.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .bars import Candle
from .contracts import BUY, SELL, pick_leg
from .costs import Charges, Slippage, round_trip_charges
from .indicators import BULLISH, Signal

SQUARE_OFF = time(15, 0)

EXIT_CROSS = "opposite_cross"
EXIT_SQUARE_OFF = "square_off"


@dataclass(frozen=True)
class StrategyConfig:
    timeframe: int  # 5, 15 or 30
    start: time
    cutoff: time
    mode: str
    lot_size: int = 65
    lots: int = 1
    square_off: time = SQUARE_OFF
    slippage: Slippage = field(default_factory=Slippage)
    fill_fallback_minutes: int = 5
    whipsaw_bars: int = 2

    @property
    def qty(self) -> int:
        return self.lot_size * self.lots


class CandleBook:
    """1m candles per symbol with exact and 'latest before' lookups."""

    def __init__(self, data: dict[str, Iterable[Candle]] | None = None):
        self._ts: dict[str, list[datetime]] = {}
        self._c: dict[str, list[Candle]] = {}
        for sym, cs in (data or {}).items():
            self.add(sym, cs)

    def reset(self, data: dict[str, Iterable[Candle]] | None = None) -> None:
        self._ts.clear()
        self._c.clear()
        for sym, cs in (data or {}).items():
            self.add(sym, cs)

    def add(self, symbol: str, candles: Iterable[Candle]) -> None:
        merged = {c.ts: c for c in self._c.get(symbol, [])}
        merged.update({c.ts: c for c in candles})
        ordered = [merged[k] for k in sorted(merged)]
        self._c[symbol] = ordered
        self._ts[symbol] = [c.ts for c in ordered]

    def has(self, symbol: str) -> bool:
        return bool(self._c.get(symbol))

    def at(self, symbol: str, ts: datetime) -> Candle | None:
        tss = self._ts.get(symbol, [])
        i = bisect_left(tss, ts)
        if i < len(tss) and tss[i] == ts:
            return self._c[symbol][i]
        return None

    def latest_before(self, symbol: str, ts: datetime) -> Candle | None:
        """Latest candle that started strictly before ``ts``."""
        tss = self._ts.get(symbol, [])
        i = bisect_left(tss, ts)
        return self._c[symbol][i - 1] if i > 0 else None

    def latest_at_or_before(self, symbol: str, ts: datetime) -> Candle | None:
        tss = self._ts.get(symbol, [])
        i = bisect_right(tss, ts)
        return self._c[symbol][i - 1] if i > 0 else None

    def candles(self, symbol: str) -> list[Candle]:
        return list(self._c.get(symbol, []))


@dataclass(frozen=True)
class Fill:
    price: float
    ts: datetime  # candle actually used
    how: str  # "open" | "fallback" | "stale"


def fill_price(
    book: CandleBook, symbol: str, at: datetime, fallback_min: int
) -> Fill | None:
    c = book.at(symbol, at)
    if c is not None:
        return Fill(c.open, c.ts, "open")
    prev = book.latest_before(symbol, at)
    if prev is not None and at - prev.ts <= timedelta(minutes=fallback_min):
        return Fill(prev.close, prev.ts, "fallback")
    return None


# resolver(day, opt_type, strike) -> (symbol, expiry) or None
Resolver = Callable[[date, str, int], "tuple[str, date] | None"]


@dataclass
class Trade:
    mode: str
    timeframe: int
    start: time
    cutoff: time
    day: date
    direction: int  # BULLISH / BEARISH view on NIFTY
    side: str  # BUY / SELL (entry order on the option)
    symbol: str
    opt_type: str
    strike: int
    expiry: date
    qty: int
    cross_no: int
    entry_signal_time: datetime
    entry_time: datetime
    entry_bar_index: int
    entry_raw: float
    entry_fill: float
    entry_how: str
    spot_entry: float | None
    reversal_entry: bool
    exit_signal_time: datetime | None = None
    exit_time: datetime | None = None
    exit_raw: float | None = None
    exit_fill: float | None = None
    exit_how: str | None = None
    exit_reason: str | None = None
    spot_exit: float | None = None
    whipsaw: bool = False
    gross: float = 0.0
    slippage: float = 0.0
    charges: Charges | None = None
    net: float = 0.0

    @property
    def dte(self) -> int:
        return (self.expiry - self.day).days

    @property
    def friction(self) -> float:
        return round(self.slippage + (self.charges.total if self.charges else 0.0), 2)

    @property
    def outcome(self) -> str:
        if self.net > 0:
            return "win"
        if self.net < 0:
            return "loss"
        return "flat"


@dataclass(frozen=True)
class Event:
    """Something worth showing in the per-day log (e.g. a skipped entry)."""

    day: date
    time: datetime
    kind: str  # no_contract | no_option_data | stale_exit
    detail: str


def settle(trade: Trade, cfg: StrategyConfig) -> None:
    """Compute fills, slippage, charges and PnL once exit_raw is known."""
    q = trade.qty
    s = cfg.slippage
    if trade.side == BUY:
        trade.entry_fill = s.buy(trade.entry_raw)
        trade.exit_fill = s.sell(trade.exit_raw)
        gross = (trade.exit_raw - trade.entry_raw) * q
        after_slip = (trade.exit_fill - trade.entry_fill) * q
        buy_value, sell_value = trade.entry_fill * q, trade.exit_fill * q
    else:
        trade.entry_fill = s.sell(trade.entry_raw)
        trade.exit_fill = s.buy(trade.exit_raw)
        gross = (trade.entry_raw - trade.exit_raw) * q
        after_slip = (trade.entry_fill - trade.exit_fill) * q
        buy_value, sell_value = trade.exit_fill * q, trade.entry_fill * q
    trade.gross = round(gross, 2)
    trade.slippage = round(gross - after_slip, 2)
    trade.charges = round_trip_charges(buy_value, sell_value, trade.day)
    trade.net = round(trade.gross - trade.slippage - trade.charges.total, 2)


class Strategy:
    """Feeds on crossover signals; produces trades for one config."""

    def __init__(
        self,
        cfg: StrategyConfig,
        options: CandleBook,
        resolver: Resolver,
        spot: CandleBook | None = None,
        spot_symbol: str = "NSE-NIFTY",
    ):
        self.cfg = cfg
        self.options = options
        self.resolver = resolver
        self.spot = spot
        self.spot_symbol = spot_symbol
        self.position: Trade | None = None
        self.trades: list[Trade] = []
        self.events: list[Event] = []
        self._cross_count: dict[date, int] = {}

    # -- helpers ---------------------------------------------------------
    def in_window(self, t: datetime) -> bool:
        return self.cfg.start <= t.time() <= self.cfg.cutoff

    def _spot_at(self, t: datetime) -> float | None:
        if self.spot is None:
            return None
        c = self.spot.at(self.spot_symbol, t)
        if c is not None:
            return c.open
        c = self.spot.latest_before(self.spot_symbol, t)
        return c.close if c is not None else None

    # -- driving ---------------------------------------------------------
    def on_signal(self, sig: Signal) -> None:
        pos = self.position
        squareoff_at = datetime.combine(sig.day, self.cfg.square_off)
        if pos is not None and (pos.day != sig.day or sig.time >= squareoff_at):
            self.square_off(pos.day)
            pos = None
        if sig.time >= squareoff_at:
            return
        in_win = self.in_window(sig.time)
        cross_no = 0
        if in_win:
            cross_no = self._cross_count.get(sig.day, 0) + 1
            self._cross_count[sig.day] = cross_no
        reversing = False
        if pos is not None and pos.direction != sig.direction:
            self._close(pos, sig.time, EXIT_CROSS, sig)
            reversing = True
        if in_win and self.position is None:
            self._open(sig, cross_no, reversing)

    def square_off(self, day: date) -> None:
        pos = self.position
        if pos is not None and pos.day == day:
            self._close(
                pos, datetime.combine(day, self.cfg.square_off), EXIT_SQUARE_OFF
            )

    def finish(self) -> None:
        if self.position is not None:
            self.square_off(self.position.day)

    # -- internals -------------------------------------------------------
    def _open(self, sig: Signal, cross_no: int, reversing: bool) -> None:
        cfg = self.cfg
        leg = pick_leg(cfg.mode, sig.direction, sig.spot)
        resolved = self.resolver(sig.day, leg.opt_type, leg.strike)
        if resolved is None:
            self.events.append(
                Event(
                    sig.day,
                    sig.time,
                    "no_contract",
                    f"No {leg.strike} {leg.opt_type} contract found",
                )
            )
            return
        symbol, expiry = resolved
        fill = fill_price(self.options, symbol, sig.time, cfg.fill_fallback_minutes)
        if fill is None:
            self.events.append(
                Event(
                    sig.day,
                    sig.time,
                    "no_option_data",
                    f"No price for {symbol} at {sig.time:%H:%M}; entry skipped",
                )
            )
            return
        self.position = Trade(
            mode=cfg.mode,
            timeframe=cfg.timeframe,
            start=cfg.start,
            cutoff=cfg.cutoff,
            day=sig.day,
            direction=sig.direction,
            side=leg.side,
            symbol=symbol,
            opt_type=leg.opt_type,
            strike=leg.strike,
            expiry=expiry,
            qty=cfg.qty,
            cross_no=cross_no,
            entry_signal_time=sig.time,
            entry_time=sig.time,
            entry_bar_index=sig.bar_index,
            entry_raw=fill.price,
            entry_fill=fill.price,
            entry_how=fill.how,
            spot_entry=self._spot_at(sig.time),
            reversal_entry=reversing,
        )

    def _close(
        self, pos: Trade, at: datetime, reason: str, sig: Signal | None = None
    ) -> None:
        fill = fill_price(self.options, pos.symbol, at, self.cfg.fill_fallback_minutes)
        if fill is None:
            # Price gap longer than the fallback: use the last traded price.
            c = self.options.latest_before(pos.symbol, at)
            if c is None:  # cannot happen: the entry candle is before ``at``
                c = Candle(
                    pos.entry_time,
                    pos.entry_raw,
                    pos.entry_raw,
                    pos.entry_raw,
                    pos.entry_raw,
                )
            fill = Fill(c.close, c.ts, "stale")
            self.events.append(
                Event(
                    pos.day,
                    at,
                    "stale_exit",
                    f"{pos.symbol}: no price near {at:%H:%M}, used {c.ts:%H:%M} close",
                )
            )
        pos.exit_signal_time = sig.time if sig else None
        pos.exit_time = at
        pos.exit_raw = fill.price
        pos.exit_how = fill.how
        pos.exit_reason = reason
        pos.spot_exit = self._spot_at(at)
        pos.whipsaw = (
            reason == EXIT_CROSS
            and sig is not None
            and sig.bar_index - pos.entry_bar_index <= self.cfg.whipsaw_bars
        )
        settle(pos, self.cfg)
        self.trades.append(pos)
        self.position = None


def run_strategy(
    cfg: StrategyConfig,
    signals: Iterable[Signal],
    options: CandleBook,
    resolver: Resolver,
    spot: CandleBook | None = None,
    days: set[date] | None = None,
) -> Strategy:
    """Backtest helper: feed all signals (optionally only on ``days``)."""
    strat = Strategy(cfg, options, resolver, spot)
    for sig in signals:
        if days is not None and sig.day not in days:
            continue
        strat.on_signal(sig)
    strat.finish()
    return strat


__all__ = [
    "BULLISH",
    "BUY",
    "SELL",
    "CandleBook",
    "Event",
    "Strategy",
    "StrategyConfig",
    "Trade",
    "run_strategy",
]
