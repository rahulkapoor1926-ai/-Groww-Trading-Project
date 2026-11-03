from datetime import date

from sqlalchemy import func, select

from app import db
from app.backtest import CUTOFFS, STARTS, RunSettings, run_backtest
from app.groww_data import OrderBlockedError, ReadOnlyGroww
from app.strategy.bars import TIMEFRAMES
from app.strategy.contracts import MODES

from .fakes import FakeGroww

FROM, TO = date(2025, 9, 29), date(2025, 10, 10)
TODAY = date(2025, 11, 1)


def api(fake):
    return ReadOnlyGroww(fake, min_interval=0, sleep=lambda s: None)


def test_full_run_stores_every_combination(engine):
    fake = FakeGroww(missing_every=50)
    run_id = run_backtest(engine, api(fake), FROM, TO, RunSettings(), today=TODAY)
    with engine.connect() as conn:
        run = conn.execute(select(db.runs).where(db.runs.c.id == run_id)).one()
        assert run.status == "done"
        combos = conn.execute(
            select(
                db.trades.c.timeframe,
                db.trades.c.start,
                db.trades.c.cutoff,
                db.trades.c.mode,
            ).distinct()
        ).all()
        n_trades = conn.execute(select(func.count()).select_from(db.trades)).scalar()
        days = conn.execute(
            select(db.days.c.day).where(db.days.c.run_id == run_id)
        ).all()
        bad = conn.execute(
            select(func.count())
            .select_from(db.trades)
            .where(
                (db.trades.c.net - (db.trades.c.gross - db.trades.c.friction)) > 0.02
            )
        ).scalar()
    assert n_trades > 100
    assert len(combos) == len(TIMEFRAMES) * len(STARTS) * len(CUTOFFS) * len(MODES)
    # 10 weekdays minus the 2 Oct holiday
    assert len(days) == 9 and date(2025, 10, 2) not in {d[0] for d in days}
    assert bad == 0


def test_second_run_downloads_nothing_new(engine):
    fake = FakeGroww()
    run_backtest(engine, api(fake), FROM, TO, RunSettings(), today=TODAY)
    first = len(fake.calls)
    fake.calls.clear()
    run_backtest(engine, api(fake), FROM, TO, RunSettings(), today=TODAY)
    assert first > 0
    assert [c for c in fake.calls if c[0] == "candles"] == []


def test_trades_respect_window_and_squareoff(engine):
    run_id = run_backtest(
        engine, api(FakeGroww()), FROM, TO, RunSettings(), today=TODAY
    )
    with engine.connect() as conn:
        rows = conn.execute(select(db.trades).where(db.trades.c.run_id == run_id)).all()
    for r in rows:
        assert r.start <= f"{r.entry_signal_time:%H:%M}" <= r.cutoff
        assert r.entry_time.date() == r.exit_time.date() == r.day
        assert f"{r.exit_time:%H:%M}" <= "15:00"
        assert r.exit_time > r.entry_time
        if r.exit_reason == "square_off":
            assert f"{r.exit_time:%H:%M}" == "15:00"
        assert r.qty == 65
        assert r.expiry >= r.day and r.expiry.weekday() == 1
        assert r.entry_fill >= 0.05 and r.exit_fill >= 0.05


def test_no_overlapping_positions_per_combo(engine):
    run_id = run_backtest(
        engine, api(FakeGroww()), FROM, TO, RunSettings(), today=TODAY
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.trades)
            .where(db.trades.c.run_id == run_id)
            .order_by(db.trades.c.entry_time)
        ).all()
    last_exit = {}
    for r in rows:
        key = (r.timeframe, r.start, r.cutoff, r.mode)
        if key in last_exit:
            assert r.entry_time >= last_exit[key]
        last_exit[key] = r.exit_time


def test_readonly_wrapper_blocks_orders():
    w = ReadOnlyGroww(FakeGroww(), min_interval=0)
    for name in ("place_order", "modify_order", "cancel_order", "create_smart_order"):
        try:
            getattr(w, name)
        except OrderBlockedError:
            continue
        raise AssertionError(f"{name} was reachable")


def test_retry_then_success():
    class Flaky(FakeGroww):
        n = 0

        def get_expiries(self, *a, **kw):
            self.n += 1
            if self.n < 3:
                raise ConnectionError("blip")
            return super().get_expiries(*a, **kw)

    sleeps = []
    w = ReadOnlyGroww(Flaky(), min_interval=0, sleep=sleeps.append)
    assert w.get_expiries(exchange="NSE", underlying_symbol="NIFTY", year=2025, month=9)
    assert sleeps == [1.0, 2.0]
