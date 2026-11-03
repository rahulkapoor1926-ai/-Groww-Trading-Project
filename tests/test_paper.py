from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from app import db
from app.backtest import RunSettings, run_backtest
from app.groww_data import ReadOnlyGroww
from app.paper import LIVE_TRADING_ENABLED, PaperEngine

from .fakes import FakeGroww

DAY = date(2025, 10, 8)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def trades_of(engine, run_id):
    with engine.connect() as conn:
        rows = conn.execute(select(db.trades).where(db.trades.c.run_id == run_id)).all()
    return sorted(
        (r.mode, r.symbol, r.entry_time, r.exit_time, r.exit_reason, r.net)
        for r in rows
    )


def test_paper_day_matches_backtest(engine):
    clock = Clock(datetime.combine(DAY, time(9, 0)))
    fake = FakeGroww(now=clock)
    api = ReadOnlyGroww(fake, min_interval=0, sleep=lambda s: None)
    paper = PaperEngine(engine, lambda: api, clock=clock)
    cfg = dict(timeframe=5, start="09:30", cutoff="14:30")
    paper.config = paper.validate(cfg)
    paper.step()
    assert paper.status == "waiting_open"
    t = datetime.combine(DAY, time(9, 16))
    while t <= datetime.combine(DAY, time(15, 6)):
        clock.t = t
        paper.step()
        t += timedelta(minutes=1)
    assert paper.status == "done"
    paper_trades = trades_of(engine, paper.run_id)
    assert paper_trades, "expected some paper trades"

    bt = run_backtest(
        engine,
        ReadOnlyGroww(FakeGroww(), min_interval=0, sleep=lambda s: None),
        DAY,
        DAY,
        RunSettings(),
        today=date(2025, 11, 1),
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.trades).where(
                db.trades.c.run_id == bt,
                db.trades.c.timeframe == 5,
                db.trades.c.start == "09:30",
                db.trades.c.cutoff == "14:30",
            )
        ).all()
    expected = sorted(
        (r.mode, r.symbol, r.entry_time, r.exit_time, r.exit_reason, r.net)
        for r in rows
    )
    assert paper_trades == expected


def test_paper_restart_replays_same_day(engine):
    clock = Clock(datetime.combine(DAY, time(12, 0)))
    api = ReadOnlyGroww(FakeGroww(now=clock), min_interval=0, sleep=lambda s: None)
    p1 = PaperEngine(engine, lambda: api, clock=clock)
    p1.config = p1.validate(dict(timeframe=5))
    p1.step()
    first = trades_of(engine, p1.run_id)
    p2 = PaperEngine(engine, lambda: api, clock=clock)
    p2.config = p2.validate(dict(timeframe=5))
    p2.step()
    assert p2.run_id == p1.run_id
    assert trades_of(engine, p2.run_id) == first
    snap = p2.snapshot()
    assert snap["live_trading_enabled"] is False


def test_weekend_is_closed(engine):
    clock = Clock(datetime(2025, 10, 11, 11, 0))  # Saturday
    p = PaperEngine(engine, lambda: None, clock=clock)
    p.config = p.validate({})
    p.step()
    assert p.status == "market_closed"


def test_validate_rejects_bad_config():
    import pytest

    for bad in (
        dict(timeframe=7),
        dict(start="12:00", cutoff="11:00"),
        dict(start="09:00"),
        dict(cutoff="15:10"),
    ):
        with pytest.raises(ValueError):
            PaperEngine.validate(bad)


def test_no_order_code_anywhere():
    assert LIVE_TRADING_ENABLED is False
    src = Path(__file__).resolve().parents[1] / "app"
    banned = (
        "place_order(",
        "modify_order(",
        "cancel_order(",
        "create_smart_order(",
        "modify_smart_order(",
        "cancel_smart_order(",
    )
    for f in src.rglob("*.py"):
        text = f.read_text()
        for b in banned:
            assert b not in text, f"{b} found in {f}"
