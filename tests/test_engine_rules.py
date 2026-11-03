"""Every trade rule of the strategy, with hand-made signals and prices."""

from datetime import date, datetime, time, timedelta

import pytest

from app.strategy.bars import Candle
from app.strategy.contracts import ATM_BUY, ATM_SELL, ITM5_BUY, option_symbol
from app.strategy.costs import Slippage, round_trip_charges
from app.strategy.engine import (
    EXIT_CROSS,
    EXIT_SQUARE_OFF,
    CandleBook,
    Strategy,
    StrategyConfig,
)
from app.strategy.indicators import BEARISH, BULLISH, Signal

D = date(2025, 9, 24)
EXP = date(2025, 9, 30)


def at(hh, mm, day=D):
    return datetime.combine(day, time(hh, mm))


def sig(hh, mm, direction, spot=24610.0, idx=None, day=D):
    t = at(hh, mm, day)
    return Signal(
        bar_index=idx if idx is not None else hh * 60 + mm,
        time=t,
        direction=direction,
        spot=spot,
        ema_fast=0,
        ema_slow=0,
    )


def resolver(day, opt_type, strike):
    return option_symbol(EXP, strike, opt_type), EXP


def flat_book(prices: dict[str, float], start=(9, 15), end=(15, 30), day=D, skip=()):
    """Every symbol trades every minute at a price that rises 0.1/min."""
    book = CandleBook()
    for sym, base in prices.items():
        cs = []
        t = at(*start, day)
        i = 0
        while t < at(*end, day):
            if t not in skip:
                p = round(base + 0.1 * i, 2)
                cs.append(Candle(t, p, p + 1, p - 1, p + 0.05))
            t += timedelta(minutes=1)
            i += 1
        book.add(sym, cs)
    return book


CE = option_symbol(EXP, 24600, "CE")
PE = option_symbol(EXP, 24600, "PE")
CE_ITM = option_symbol(EXP, 24350, "CE")
PE_ITM = option_symbol(EXP, 24850, "PE")
ALL = {CE: 100.0, PE: 90.0, CE_ITM: 300.0, PE_ITM: 280.0}


def cfg(mode=ATM_BUY, start=time(9, 30), cutoff=time(14, 30), tf=15, **kw):
    return StrategyConfig(timeframe=tf, start=start, cutoff=cutoff, mode=mode, **kw)


def price(book, sym, hh, mm):
    return book.at(sym, at(hh, mm)).open


def test_entry_fills_at_open_of_option_candle_at_signal_time():
    book = flat_book(ALL)
    s = Strategy(cfg(), book, resolver)
    s.on_signal(sig(10, 0, BULLISH))
    p = s.position
    assert p.symbol == CE and p.side == "BUY" and p.entry_time == at(10, 0)
    assert p.entry_raw == price(book, CE, 10, 0)
    assert p.entry_how == "open"


@pytest.mark.parametrize(
    "hh,mm,opens",
    [(9, 30, True), (9, 29, False), (14, 30, True), (14, 31, False), (12, 0, True)],
)
def test_entry_window_is_inclusive(hh, mm, opens):
    s = Strategy(cfg(), flat_book(ALL), resolver)
    s.on_signal(sig(hh, mm, BULLISH))
    assert (s.position is not None) == opens


def test_opposite_cross_inside_window_reverses_with_fresh_strike():
    book = flat_book({**ALL, option_symbol(EXP, 24700, "PE"): 95.0})
    s = Strategy(cfg(), book, resolver)
    s.on_signal(sig(10, 0, BULLISH, spot=24610))
    s.on_signal(sig(11, 0, BEARISH, spot=24690))
    assert len(s.trades) == 1
    t = s.trades[0]
    assert t.exit_reason == EXIT_CROSS and t.exit_time == at(11, 0)
    assert t.exit_raw == price(book, CE, 11, 0)
    p = s.position
    assert p.opt_type == "PE" and p.strike == 24700 and p.entry_time == at(11, 0)
    assert p.reversal_entry is True
    assert p.cross_no == 2


def test_opposite_cross_after_cutoff_closes_only():
    s = Strategy(cfg(cutoff=time(11, 30)), flat_book(ALL), resolver)
    s.on_signal(sig(10, 0, BULLISH))
    s.on_signal(sig(13, 0, BEARISH))
    assert s.position is None
    assert len(s.trades) == 1 and s.trades[0].exit_reason == EXIT_CROSS


def test_trade_open_at_cutoff_is_held_until_square_off():
    book = flat_book(ALL)
    s = Strategy(cfg(cutoff=time(11, 30)), book, resolver)
    s.on_signal(sig(11, 30, BULLISH))
    s.square_off(D)
    t = s.trades[0]
    assert t.exit_reason == EXIT_SQUARE_OFF
    assert t.exit_time == at(15, 0)
    assert t.exit_raw == price(book, CE, 15, 0)


def test_signal_at_1500_squares_off_and_later_signals_ignored():
    s = Strategy(cfg(), flat_book(ALL), resolver)
    s.on_signal(sig(14, 0, BULLISH))
    s.on_signal(sig(15, 0, BEARISH))
    assert s.trades[0].exit_reason == EXIT_SQUARE_OFF
    s.on_signal(sig(15, 15, BULLISH))
    assert s.position is None and len(s.trades) == 1


def test_open_position_squared_off_when_next_day_signal_arrives():
    d2 = D + timedelta(days=1)
    book = flat_book(ALL)
    book.add(CE, flat_book({CE: 1}, day=d2).candles(CE))
    s = Strategy(cfg(), book, resolver)
    s.on_signal(sig(14, 0, BULLISH))
    s.on_signal(sig(10, 0, BEARISH, day=d2))
    assert s.trades[0].exit_time == at(15, 0) and s.trades[0].day == D


def test_missing_candle_uses_close_within_5_minutes():
    book = flat_book(ALL, skip={at(10, 0), at(10, 1)})
    s = Strategy(cfg(), book, resolver)
    s.on_signal(sig(10, 0, BULLISH))
    p = s.position
    prev = book.at(CE, at(9, 59))
    assert p.entry_how == "fallback" and p.entry_raw == prev.close


def test_missing_option_data_skips_entry_and_logs():
    gap = {at(9, 54) + timedelta(minutes=i) for i in range(10)}
    book = flat_book(ALL, skip=gap)  # nothing from 09:54 to 10:03
    s = Strategy(cfg(), book, resolver)
    s.on_signal(sig(10, 0, BULLISH))
    assert s.position is None
    assert s.events[0].kind == "no_option_data"


def test_unknown_contract_is_logged():
    s = Strategy(cfg(), flat_book(ALL), lambda d, o, k: None)
    s.on_signal(sig(10, 0, BULLISH))
    assert s.position is None and s.events[0].kind == "no_contract"


def test_stale_exit_uses_last_traded_price():
    gap = {at(14, 30) + timedelta(minutes=i) for i in range(60)}
    book = flat_book(ALL, skip=gap)
    s = Strategy(cfg(), book, resolver)
    s.on_signal(sig(10, 0, BULLISH))
    s.square_off(D)
    t = s.trades[0]
    assert t.exit_how == "stale" and t.exit_raw == book.at(CE, at(14, 29)).close
    assert any(e.kind == "stale_exit" for e in s.events)


def test_cross_number_counts_only_inside_window():
    s = Strategy(cfg(start=time(10, 0)), flat_book(ALL), resolver)
    s.on_signal(sig(9, 45, BULLISH))  # outside, not counted
    s.on_signal(sig(10, 15, BEARISH))  # 1st in window -> opens PE
    s.on_signal(sig(10, 45, BULLISH))  # 2nd -> reverse into CE
    assert [t.cross_no for t in s.trades] == [1]
    assert s.position.cross_no == 2


def test_whipsaw_flag():
    s = Strategy(cfg(), flat_book(ALL), resolver)
    s.on_signal(sig(10, 0, BULLISH, idx=10))
    s.on_signal(sig(10, 30, BEARISH, idx=12))  # 2 bars later -> whipsaw
    s.on_signal(sig(11, 30, BULLISH, idx=16))  # 4 bars later -> not
    s.square_off(D)
    assert [t.whipsaw for t in s.trades] == [True, False, False]


def test_buy_trade_pnl_breakdown():
    book = flat_book(ALL)
    c = cfg(slippage=Slippage(0.005, 0.5))
    s = Strategy(c, book, resolver)
    s.on_signal(sig(10, 0, BULLISH))
    s.square_off(D)
    t = s.trades[0]
    q = 65
    e, x = price(book, CE, 10, 0), price(book, CE, 15, 0)
    assert t.gross == round((x - e) * q, 2)
    ef, xf = c.slippage.buy(e), c.slippage.sell(x)
    assert (t.entry_fill, t.exit_fill) == (ef, xf)
    assert t.slippage == round((x - e) * q - (xf - ef) * q, 2)
    ch = round_trip_charges(ef * q, xf * q, D)
    assert t.charges == ch
    assert t.net == round(t.gross - t.slippage - ch.total, 2)
    assert t.friction == round(t.slippage + ch.total, 2)
    assert t.dte == 6


def test_sell_mode_profits_when_premium_falls():
    book = CandleBook()
    falling = [
        Candle(
            at(9, 15) + timedelta(minutes=i),
            200 - i * 0.5,
            200 - i * 0.5,
            200 - i * 0.5,
            200 - i * 0.5,
        )
        for i in range(375)
    ]
    book.add(PE, falling)
    s = Strategy(cfg(mode=ATM_SELL), book, resolver)
    s.on_signal(sig(10, 0, BULLISH))  # bullish -> sell PE
    assert s.position.side == "SELL" and s.position.symbol == PE
    s.square_off(D)
    t = s.trades[0]
    e, x = book.at(PE, at(10, 0)).open, book.at(PE, at(15, 0)).open
    assert t.gross == round((e - x) * 65, 2) > 0
    # sold lower and bought back higher because of slippage
    assert t.entry_fill < e and t.exit_fill > x
    assert t.net < t.gross


def test_itm5_mode_strikes():
    s = Strategy(cfg(mode=ITM5_BUY), flat_book(ALL), resolver)
    s.on_signal(sig(10, 0, BULLISH))
    assert s.position.symbol == CE_ITM
    s.on_signal(sig(10, 30, BEARISH))
    assert s.position.symbol == PE_ITM


def test_lots_multiply_quantity():
    s = Strategy(cfg(lots=2), flat_book(ALL), resolver)
    s.on_signal(sig(10, 0, BULLISH))
    assert s.position.qty == 130


def test_spot_prices_recorded():
    spot = CandleBook(
        {
            "NSE-NIFTY": [
                Candle(at(10, 0), 24611, 24611, 24611, 24611),
                Candle(at(14, 59), 24700, 24700, 24700, 24705),
            ]
        }
    )
    s = Strategy(cfg(), flat_book(ALL), resolver, spot)
    s.on_signal(sig(10, 0, BULLISH))
    s.square_off(D)
    t = s.trades[0]
    assert (
        t.spot_entry == 24611 and t.spot_exit == 24705
    )  # 15:00 missing -> 14:59 close
