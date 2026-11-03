"""Download data and run the full grid of backtests.

A run covers every timeframe x start x cutoff x mode, so the dashboard filters
only need to query stored trades.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from . import db
from .groww_data import (
    SEG_CASH,
    SEG_FNO,
    SPOT_SYMBOL,
    UNDERLYING,
    ReadOnlyGroww,
    day_chunks,
    fetch_candles,
    fetch_contracts,
    fetch_expiries,
)
from .strategy.bars import TIMEFRAMES, Bar, resample, session_close, session_open
from .strategy.contracts import MODES, nearest_expiry, option_symbol, pick_leg
from .strategy.costs import Slippage
from .strategy.engine import CandleBook, Strategy, StrategyConfig, Trade
from .strategy.indicators import Signal, crossovers

STARTS = (time(9, 30), time(10, 0), time(10, 30))
CUTOFFS = tuple(
    time(h, m)
    for h, m in [(11, 30), (12, 0), (12, 30), (13, 0), (13, 30), (14, 0), (14, 30)]
)
WARMUP_DAYS = 10  # calendar days of extra spot history for the EMAs
FULL_DAY_MINUTES = 375
INCOMPLETE_BELOW = 0.8  # a day with < 80% of its minutes is flagged

Progress = Callable[[str, int, int, str], None]


def ist_today() -> date:
    from .groww_client import IST

    return datetime.now(IST).date()


def _noop(stage: str, done: int, total: int, msg: str) -> None:
    pass


@dataclass(frozen=True)
class RunSettings:
    slippage_pct: float = 0.005
    slippage_points: float = 0.5
    lot_size: int = 65
    lots: int = 1
    capital: float = 200_000.0

    @classmethod
    def from_dict(cls, d: dict | None) -> RunSettings:
        d = d or {}
        return cls(
            slippage_pct=float(d.get("slippage_pct", cls.slippage_pct)),
            slippage_points=float(d.get("slippage_points", cls.slippage_points)),
            lot_size=int(d.get("lot_size", cls.lot_size)),
            lots=int(d.get("lots", cls.lots)),
            capital=float(d.get("capital", cls.capital)),
        )

    def as_dict(self) -> dict:
        return dict(
            slippage_pct=self.slippage_pct,
            slippage_points=self.slippage_points,
            lot_size=self.lot_size,
            lots=self.lots,
            capital=self.capital,
        )


def hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# Signals and contract resolution
# --------------------------------------------------------------------------
@dataclass
class SignalSet:
    bars: list[Bar]
    signals: list[Signal]
    by_day: dict[date, list[Signal]] = field(default_factory=dict)

    @classmethod
    def build(cls, spot_candles, minutes: int) -> SignalSet:
        bars = resample(spot_candles, minutes)
        sigs = crossovers(bars)
        by_day: dict[date, list[Signal]] = defaultdict(list)
        for s in sigs:
            by_day[s.day].append(s)
        return cls(bars, sigs, dict(by_day))


class ContractResolver:
    """(day, CE/PE, strike) -> (groww symbol, expiry)."""

    def __init__(
        self,
        expiry_list: list[date],
        known: dict[tuple, str],
        listed_expiries: set[date],
    ):
        self.expiries = sorted(expiry_list)
        self.known = known
        self.listed = listed_expiries  # expiries whose full contract list we have

    def expiry_for(self, day: date) -> date | None:
        return nearest_expiry(day, self.expiries)

    def __call__(self, day: date, opt_type: str, strike: int):
        exp = self.expiry_for(day)
        if exp is None:
            return None
        if exp in self.listed:
            sym = self.known.get((exp, int(strike), opt_type))
            return (sym, exp) if sym else None
        return option_symbol(exp, strike, opt_type), exp


def needed_legs(signal_sets: dict[int, SignalSet], days: set[date]) -> set[tuple]:
    """Every (day, opt_type, strike) any combo could trade."""
    widest_start, widest_cut = min(STARTS), max(CUTOFFS)
    legs = set()
    for ss in signal_sets.values():
        for d in days:
            for s in ss.by_day.get(d, ()):
                if not (widest_start <= s.time.time() <= widest_cut):
                    continue
                for mode in MODES:
                    leg = pick_leg(mode, s.direction, s.spot)
                    legs.add((s.day, leg.opt_type, leg.strike))
    return legs


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------
def download(
    engine: Engine,
    api: ReadOnlyGroww,
    date_from: date,
    date_to: date,
    progress: Progress = _noop,
    today: date | None = None,
) -> dict:
    """Fetch everything a run over [date_from, date_to] needs. Resumable."""
    today = today or ist_today()
    date_to = min(date_to, today)
    stats = {"spot_requests": 0, "option_requests": 0, "option_rows": 0}

    # 1. Spot 1m candles (plus warm-up history)
    spot_from = date_from - timedelta(days=WARMUP_DAYS)
    have = db.covered_days(engine, SPOT_SYMBOL, spot_from, date_to)
    missing = [d for d in weekdays(spot_from, date_to) if d not in have or d >= today]
    chunks = [
        c
        for c in day_chunks(spot_from, date_to)
        if any(c[0] <= d <= c[1] for d in missing)
    ]
    for i, (a, b) in enumerate(chunks):
        progress("spot", i, len(chunks), f"NIFTY 1m candles {a} to {b}")
        rows = fetch_candles(
            api, SPOT_SYMBOL, SEG_CASH, session_open(a), session_close(b)
        )
        stats["spot_requests"] += 1
        db.upsert_candles(engine, SPOT_SYMBOL, rows)
        counts: dict[date, int] = defaultdict(int)
        for r in rows:
            counts[r[0].date()] += 1
        # Only past days are final; today can still grow.
        db.mark_covered(
            engine,
            SPOT_SYMBOL,
            {d: counts.get(d, 0) for d in weekdays(a, b) if d < today},
        )
    progress("spot", len(chunks), len(chunks), "NIFTY candles ready")

    # 2. Expiry calendar (one extra month so late-month days find an expiry)
    months = set()
    d = date(date_from.year, date_from.month, 1)
    last = date(date_to.year, date_to.month, 1) + timedelta(days=62)
    while d <= last:
        months.add((d.year, d.month))
        d = (d + timedelta(days=32)).replace(day=1)
    fetched = db.fetched_expiry_months(engine, UNDERLYING)
    cur_month = (today.year, today.month)
    todo = sorted(m for m in months if m not in fetched or m >= cur_month)
    for i, (y, m) in enumerate(todo):
        progress("expiries", i, len(todo), f"Expiry calendar {y}-{m:02d}")
        db.save_expiries(engine, UNDERLYING, y, m, fetch_expiries(api, y, m))

    # 3. Work out which option contracts the strategy can touch
    spot = db.load_candles(
        engine, SPOT_SYMBOL, session_open(spot_from), session_close(date_to)
    )
    days = {c.ts.date() for c in spot if date_from <= c.ts.date() <= date_to}
    sets = {tf: SignalSet.build(spot, tf) for tf in TIMEFRAMES}
    legs = needed_legs(sets, days)
    expiry_list = db.load_expiries(engine, UNDERLYING)
    exp_needed = sorted(
        {e for e in (nearest_expiry(dd, expiry_list) for dd, _, _ in legs) if e}
    )
    listed = db.fetched_contract_lists(engine, UNDERLYING)
    todo_exp = [e for e in exp_needed if e not in listed]
    for i, e in enumerate(todo_exp):
        progress("contracts", i, len(todo_exp), f"Contracts for expiry {e}")
        try:
            rows = fetch_contracts(api, e)
        except Exception:
            rows = []
        if rows:
            db.save_contracts(engine, UNDERLYING, e, rows)

    resolver = load_resolver(engine)
    wanted: set[tuple[str, date]] = set()
    for dd, opt_type, strike in legs:
        r = resolver(dd, opt_type, strike)
        if r:
            wanted.add((r[0], dd))
    have_pairs = db.covered_pairs(engine, sorted({s for s, _ in wanted}))
    todo_pairs = sorted(p for p in wanted if p not in have_pairs or p[1] >= today)
    stats["option_contracts"] = len(wanted)

    # 4. Option 1m candles, one request per contract-day
    for i, (sym, dd) in enumerate(todo_pairs):
        if i % 5 == 0:
            progress("options", i, len(todo_pairs), f"{sym} on {dd}")
        try:
            rows = fetch_candles(api, sym, SEG_FNO, session_open(dd), session_close(dd))
        except Exception as exc:
            if "not found" in str(exc).lower():
                rows = []
            else:
                raise
        stats["option_requests"] += 1
        stats["option_rows"] += len(rows)
        db.upsert_candles(engine, sym, rows)
        if dd < today:
            db.mark_covered(engine, sym, {dd: len(rows)})
    progress("options", len(todo_pairs), len(todo_pairs), "Option candles ready")
    return stats


def load_resolver(engine: Engine) -> ContractResolver:
    exps = db.load_expiries(engine, UNDERLYING)
    listed = db.fetched_contract_lists(engine, UNDERLYING)
    return ContractResolver(exps, db.load_contracts(engine, listed), listed)


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
def all_configs(settings: RunSettings) -> list[StrategyConfig]:
    slip = Slippage(settings.slippage_pct, settings.slippage_points)
    return [
        StrategyConfig(
            timeframe=tf,
            start=s,
            cutoff=c,
            mode=m,
            lot_size=settings.lot_size,
            lots=settings.lots,
            slippage=slip,
        )
        for tf in TIMEFRAMES
        for s in STARTS
        for c in CUTOFFS
        for m in MODES
    ]


def trade_row(run_id: int, t: Trade) -> dict:
    ch = t.charges
    return dict(
        run_id=run_id,
        timeframe=t.timeframe,
        start=hhmm(t.start),
        cutoff=hhmm(t.cutoff),
        mode=t.mode,
        day=t.day,
        direction=t.direction,
        side=t.side,
        symbol=t.symbol,
        opt_type=t.opt_type,
        strike=t.strike,
        expiry=t.expiry,
        dte=t.dte,
        qty=t.qty,
        cross_no=t.cross_no,
        entry_signal_time=t.entry_signal_time,
        entry_time=t.entry_time,
        entry_raw=t.entry_raw,
        entry_fill=t.entry_fill,
        entry_how=t.entry_how,
        spot_entry=t.spot_entry,
        reversal_entry=t.reversal_entry,
        exit_signal_time=t.exit_signal_time,
        exit_time=t.exit_time,
        exit_raw=t.exit_raw,
        exit_fill=t.exit_fill,
        exit_how=t.exit_how,
        exit_reason=t.exit_reason,
        spot_exit=t.spot_exit,
        whipsaw=t.whipsaw,
        gross=t.gross,
        slippage=t.slippage,
        brokerage=ch.brokerage,
        stt=ch.stt,
        exchange=ch.exchange,
        sebi=ch.sebi,
        stamp=ch.stamp,
        gst=ch.gst,
        charges=ch.total,
        friction=t.friction,
        net=t.net,
    )


def simulate(
    engine: Engine,
    date_from: date,
    date_to: date,
    settings: RunSettings,
    progress: Progress = _noop,
) -> tuple[list[Strategy], dict[date, int]]:
    """Run every config over the stored data. Returns strategies and spot
    minutes per day."""
    spot_from = date_from - timedelta(days=WARMUP_DAYS)
    spot = db.load_candles(
        engine, SPOT_SYMBOL, session_open(spot_from), session_close(date_to)
    )
    spot_book = CandleBook({SPOT_SYMBOL: spot})
    minutes: dict[date, int] = defaultdict(int)
    for c in spot:
        if date_from <= c.ts.date() <= date_to:
            minutes[c.ts.date()] += 1
    days = sorted(minutes)
    sets = {tf: SignalSet.build(spot, tf) for tf in TIMEFRAMES}
    resolver = load_resolver(engine)

    options = CandleBook()
    strategies = [
        Strategy(cfg, options, resolver, spot_book) for cfg in all_configs(settings)
    ]
    for i, d in enumerate(days):
        if i % 5 == 0:
            progress("simulate", i, len(days), f"Simulating {d}")
        legs = needed_legs(sets, {d})
        syms = sorted({r[0] for r in (resolver(*leg) for leg in legs) if r})
        data = (
            db.load_candles_multi(engine, syms, session_open(d), session_close(d))
            if syms
            else {}
        )
        options.reset(data)  # swap in this day's option candles
        for strat in strategies:
            for sig in sets[strat.cfg.timeframe].by_day.get(d, []):
                strat.on_signal(sig)
            strat.square_off(d)
    progress("simulate", len(days), len(days), "Simulation done")
    return strategies, dict(minutes)


def create_run(
    engine: Engine,
    date_from: date,
    date_to: date,
    settings: RunSettings,
    kind: str = "backtest",
) -> int:
    with engine.begin() as conn:
        res = conn.execute(
            insert(db.runs).values(
                kind=kind,
                created_at=datetime.now(),
                date_from=date_from,
                date_to=date_to,
                params=settings.as_dict(),
                status="running",
                message=None,
            )
        )
        return int(res.inserted_primary_key[0])


def finish_run(engine: Engine, run_id: int, status: str, message: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(db.runs)
            .where(db.runs.c.id == run_id)
            .values(status=status, message=message)
        )


def save_results(
    engine: Engine, run_id: int, strategies: list[Strategy], minutes: dict[date, int]
) -> int:
    rows = []
    evs = []
    for s in strategies:
        rows.extend(trade_row(run_id, t) for t in s.trades)
        c = s.cfg
        evs.extend(
            dict(
                run_id=run_id,
                timeframe=c.timeframe,
                start=hhmm(c.start),
                cutoff=hhmm(c.cutoff),
                mode=c.mode,
                day=e.day,
                time=e.time,
                kind=e.kind,
                detail=e.detail,
            )
            for e in s.events
        )
    day_rows = [
        dict(
            run_id=run_id,
            day=d,
            minutes=n,
            status="ok" if n >= FULL_DAY_MINUTES * INCOMPLETE_BELOW else "incomplete",
        )
        for d, n in sorted(minutes.items())
    ]
    with engine.begin() as conn:
        db.delete_run_rows(conn, run_id)
        for table, data in ((db.trades, rows), (db.events, evs), (db.days, day_rows)):
            for i in range(0, len(data), 5000):
                conn.execute(insert(table), data[i : i + 5000])
    return len(rows)


def run_backtest(
    engine: Engine,
    api: ReadOnlyGroww | None,
    date_from: date,
    date_to: date,
    settings: RunSettings,
    progress: Progress = _noop,
    today: date | None = None,
) -> int:
    """Download (if ``api`` given) then simulate and store. Returns run id."""
    if date_from > date_to:
        raise ValueError("Start date must be on or before end date.")
    if api is not None:
        download(engine, api, date_from, date_to, progress, today=today)
    run_id = create_run(engine, date_from, date_to, settings)
    try:
        strategies, minutes = simulate(engine, date_from, date_to, settings, progress)
        if not minutes:
            raise ValueError(
                "No NIFTY data stored for this date range. Download it first."
            )
        n = save_results(engine, run_id, strategies, minutes)
        finish_run(
            engine, run_id, "done", f"{n} trades across {len(strategies)} combinations"
        )
    except Exception as exc:
        finish_run(engine, run_id, "failed", str(exc))
        raise
    return run_id


def latest_run_id(engine: Engine, kind: str = "backtest") -> int | None:
    q = (
        select(db.runs.c.id)
        .where(db.runs.c.kind == kind, db.runs.c.status == "done")
        .order_by(db.runs.c.id.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        return conn.execute(q).scalar()
