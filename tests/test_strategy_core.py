from datetime import date, datetime, time

import pytest

from app.strategy.bars import Candle, bucket_start, resample
from app.strategy.contracts import (
    ATM_BUY,
    ATM_SELL,
    ITM5_BUY,
    Leg,
    atm_strike,
    nearest_expiry,
    option_symbol,
    parse_option_symbol,
    pick_leg,
)
from app.strategy.costs import (
    Slippage,
    rates_on,
    round_down_tick,
    round_trip_charges,
    round_up_tick,
)
from app.strategy.indicators import BEARISH, BULLISH, crossovers, ema
from app.strategy.metrics import TradePnl, compute

D = date(2025, 9, 24)


def c(hh, mm, o, h=None, lo=None, cl=None, day=D):
    h = o if h is None else h
    lo = o if lo is None else lo
    cl = o if cl is None else cl
    return Candle(datetime.combine(day, time(hh, mm)), o, h, lo, cl, 1)


# ---------------------------------------------------------------- bars
def test_bucket_alignment_is_anchored_at_0915():
    assert bucket_start(datetime(2025, 9, 24, 9, 29), 15) == datetime(
        2025, 9, 24, 9, 15
    )
    assert bucket_start(datetime(2025, 9, 24, 9, 30), 15) == datetime(
        2025, 9, 24, 9, 30
    )
    assert bucket_start(datetime(2025, 9, 24, 9, 44), 30) == datetime(
        2025, 9, 24, 9, 15
    )
    assert bucket_start(datetime(2025, 9, 24, 9, 45), 30) == datetime(
        2025, 9, 24, 9, 45
    )
    assert bucket_start(datetime(2025, 9, 24, 9, 19), 5) == datetime(2025, 9, 24, 9, 15)


def test_resample_ohlcv_and_close_time():
    candles = [
        c(9, 15, 10, 12, 9, 11),
        c(9, 16, 11, 15, 10, 14),
        c(9, 19, 14, 14, 8, 9),
        c(9, 20, 9, 9, 9, 9),
    ]
    bars = resample(candles, 5)
    assert len(bars) == 2
    b = bars[0]
    assert (b.open, b.high, b.low, b.close, b.volume, b.minutes) == (10, 15, 8, 9, 3, 3)
    assert b.start.time() == time(9, 15) and b.end.time() == time(9, 20)
    assert bars[1].start.time() == time(9, 20)


def test_resample_drops_out_of_session_and_caps_last_bar():
    candles = [c(9, 14, 1), c(15, 15, 5), c(15, 29, 6), c(15, 30, 7)]
    bars = resample(candles, 30)
    assert len(bars) == 1
    assert bars[0].start.time() == time(15, 15)
    assert bars[0].end.time() == time(15, 30)
    assert bars[0].close == 6


def test_resample_keeps_days_separate_and_dedupes():
    d2 = date(2025, 9, 25)
    candles = [c(15, 29, 1), c(9, 15, 2, day=d2), c(9, 15, 3, day=d2)]
    bars = resample(candles, 15)
    assert [b.day for b in bars] == [D, d2]
    assert bars[1].close == 3 and bars[1].minutes == 1


# ---------------------------------------------------------------- ema
def test_ema_seed_and_recursion():
    vals = [1, 2, 3, 4, 5]
    out = ema(vals, 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert out[4] == pytest.approx(0.5 * 5 + 0.5 * 3.0)


def test_ema_too_short():
    assert ema([1, 2], 3) == [None, None]


def _bars_from_closes(closes):
    candles = []
    t = datetime.combine(D, time(9, 15))
    from datetime import timedelta

    for i, v in enumerate(closes):
        candles.append(Candle(t + timedelta(minutes=5 * i), v, v, v, v, 1))
    return resample(candles, 5)


def test_crossovers_detect_up_and_down():
    closes = [100.0] * 25 + [110.0] * 10 + [90.0] * 15
    bars = _bars_from_closes(closes)
    sigs = crossovers(bars)
    # Flat start: spread is 0 (a tie) so it sets no state. The jump to 110
    # makes the spread positive, which only initialises the state (no cross).
    # The drop to 90 flips it: exactly one bearish cross.
    assert [s.direction for s in sigs] == [BEARISH]
    assert sigs[0].bar_index > 35
    s = sigs[-1]
    assert s.time == bars[s.bar_index].end
    assert s.spot == bars[s.bar_index].close


def test_tie_is_not_a_cross_and_sign_carries_over(monkeypatch):
    # Drive the spread directly: +, 0, + -> no cross; +, 0, - -> cross at "-"
    import app.strategy.indicators as ind

    bars = _bars_from_closes([1.0] * 6)
    seqs = {
        "fast": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        "slow": [0.5, 1.0, 0.5, 1.0, 1.5, 1.5],
    }

    def fake_ema(values, period):
        return seqs["fast"] if period == 9 else seqs["slow"]

    monkeypatch.setattr(ind, "ema", fake_ema)
    sigs = ind.crossovers(bars)
    # spreads: +0.5, 0, +0.5, 0, -0.5, -0.5 -> exactly one bearish cross at bar 4
    assert [(s.bar_index, s.direction) for s in sigs] == [(4, BEARISH)]


def test_tie_rounding_to_paisa(monkeypatch):
    import app.strategy.indicators as ind

    bars = _bars_from_closes([1.0] * 3)
    monkeypatch.setattr(
        ind, "ema", lambda v, p: [10.0, 10.0, 10.0] if p == 9 else [9.0, 10.004, 10.02]
    )
    sigs = ind.crossovers(bars)
    # +1, 0 (|0.004| rounds to 0.00), -0.02 -> bearish cross at bar 2
    assert [(s.bar_index, s.direction) for s in sigs] == [(2, BEARISH)]


def test_no_cross_during_warmup():
    bars = _bars_from_closes([100 + (i % 2) for i in range(20)])
    assert crossovers(bars) == []  # fewer than 21 bars: EMA21 undefined


# ---------------------------------------------------------------- contracts
@pytest.mark.parametrize(
    "spot,atm",
    [(24624.99, 24600), (24625.0, 24650), (24649.0, 24650), (24574.9, 24550)],
)
def test_atm_rounding(spot, atm):
    assert atm_strike(spot) == atm


def test_pick_leg_per_mode():
    assert pick_leg(ITM5_BUY, BULLISH, 24610) == Leg("CE", 24350, "BUY")
    assert pick_leg(ITM5_BUY, BEARISH, 24610) == Leg("PE", 24850, "BUY")
    assert pick_leg(ATM_BUY, BULLISH, 24610) == Leg("CE", 24600, "BUY")
    assert pick_leg(ATM_BUY, BEARISH, 24610) == Leg("PE", 24600, "BUY")
    assert pick_leg(ATM_SELL, BULLISH, 24610) == Leg("PE", 24600, "SELL")
    assert pick_leg(ATM_SELL, BEARISH, 24610) == Leg("CE", 24600, "SELL")
    with pytest.raises(ValueError):
        pick_leg("NOPE", BULLISH, 1)


def test_nearest_expiry_includes_expiry_day():
    exps = [date(2025, 9, 23), date(2025, 9, 30), date(2025, 10, 7)]
    assert nearest_expiry(date(2025, 9, 23), exps) == date(2025, 9, 23)
    assert nearest_expiry(date(2025, 9, 24), exps) == date(2025, 9, 30)
    assert nearest_expiry(date(2025, 10, 8), exps) is None


def test_option_symbol_roundtrip():
    s = option_symbol(date(2025, 9, 30), 24650, "CE")
    assert s == "NSE-NIFTY-30Sep25-24650-CE"
    assert parse_option_symbol(s) == (date(2025, 9, 30), 24650.0, "CE")
    assert option_symbol(date(2025, 1, 2), 28500, "PE") == "NSE-NIFTY-02Jan25-28500-PE"
    assert parse_option_symbol("NSE-NIFTY") is None
    assert parse_option_symbol("NSE-NIFTY-99Foo25-1-CE") is None


# ---------------------------------------------------------------- costs
def test_tick_rounding():
    assert round_up_tick(100.05) == 100.05
    assert round_up_tick(100.051) == 100.1
    assert round_down_tick(100.099) == 100.05
    assert round_down_tick(0.3) == 0.3
    assert round_up_tick(0.1 + 0.2) == 0.3


def test_slippage_buy_sell_and_floor():
    s = Slippage(0.005, 0.5)
    assert s.buy(100) == 101.0  # 100.5 + 0.5
    assert s.sell(100) == 99.0  # 99.5 - 0.5
    assert s.buy(123.45) == round_up_tick(123.45 * 1.005 + 0.5)
    assert s.sell(0.4) == 0.05  # floored at the tick
    assert s.sell(0.05) == 0.05


def test_charges_hand_calculated_after_april_2026():
    # buy 65 @ 101 = 6565, sell 65 @ 120 = 7800, on 2026-05-05
    ch = round_trip_charges(6565.0, 7800.0, date(2026, 5, 5))
    turnover = 14365.0
    assert ch.brokerage == 40.0
    assert ch.stt == round(7800 * 0.0015, 2) == 11.7
    assert ch.exchange == round(turnover * 0.0003503, 2) == 5.03
    assert ch.sebi == round(turnover * 1e-6, 2) == 0.01
    assert ch.stamp == round(6565 * 0.00003, 2) == 0.2
    assert ch.gst == round((40 + 5.03 + 0.01) * 0.18, 2) == 8.11
    assert ch.total == round(40 + 11.7 + 5.03 + 0.01 + 0.2 + 8.11, 2)


def test_rates_by_date():
    assert rates_on(date(2024, 9, 30)).stt_sell == 0.000625
    assert rates_on(date(2024, 10, 1)).stt_sell == 0.001
    assert rates_on(date(2026, 3, 31)).stt_sell == 0.001
    assert rates_on(date(2026, 4, 1)).stt_sell == 0.0015


# ---------------------------------------------------------------- metrics
def test_metrics_hand_calculated():
    trades = [
        TradePnl(1000, 1100, 100),
        TradePnl(-500, -400, 100),
        TradePnl(-300, -200, 100),
        TradePnl(2000, 2100, 100),
    ]
    m = compute(trades, capital=100_000)
    assert m["trades"] == 4 and m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 50.0
    assert m["net"] == 2200 and m["gross"] == 2600 and m["friction"] == 400
    assert m["profit_factor"] == round(3000 / 800, 2)
    assert m["payoff"] == round(1500 / 400, 2)
    assert m["expectancy"] == 550
    # equity 101000, 100500, 100200, 102200 -> dd 800 from 101000
    assert m["max_dd"] == 800
    assert m["max_dd_pct"] == round(800 / 101000 * 100, 2)
    assert m["equity"] == [101000, 100500, 100200, 102200]


def test_metrics_empty_and_no_losses():
    assert compute([])["win_rate"] is None
    m = compute([TradePnl(10, 10, 0)])
    assert m["profit_factor"] is None and m["payoff"] is None and m["win_rate"] == 100.0
