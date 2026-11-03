"""Paper trading engine: the backtest rules, run live on real Groww prices.

There is no live-order code here or anywhere in the app. The Groww client is
wrapped in :class:`ReadOnlyGroww`, which only allows historical-data calls.

How it works: once a minute it pulls today's closed NIFTY 1m candles, rebuilds
the bars and EMAs exactly like the backtest, and feeds new crossovers to the
same :class:`Strategy` class. A signal at time T is processed at T + 2 minutes,
when the option's 1m candle that opened at T has closed, so the fill is that
candle's open, just like the backtest. Restarting the engine replays the day
from 09:15, so it always ends up in the same state.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Engine

from . import db
from .backtest import (
    WARMUP_DAYS,
    ContractResolver,
    RunSettings,
    create_run,
    finish_run,
    hhmm,
    trade_row,
    weekdays,
)
from .groww_data import (
    SEG_CASH,
    SEG_FNO,
    SPOT_SYMBOL,
    UNDERLYING,
    ReadOnlyGroww,
    fetch_candles,
    fetch_contracts,
    fetch_expiries,
)
from .strategy.bars import (
    SESSION_OPEN,
    TIMEFRAMES,
    Candle,
    resample,
    session_close,
    session_open,
)
from .strategy.contracts import MODES, nearest_expiry, pick_leg
from .strategy.costs import Slippage
from .strategy.engine import CandleBook, Strategy, StrategyConfig
from .strategy.indicators import crossovers

LIVE_TRADING_ENABLED = False  # Hard-coded. There is no switch to turn this on.
PROCESS_LAG = timedelta(minutes=2)
STOP_AFTER = time(15, 5)


def ist_now() -> datetime:
    from .groww_client import IST

    return datetime.now(IST).replace(tzinfo=None)


class PaperEngine:
    def __init__(
        self,
        engine: Engine,
        api_factory: Callable[[], ReadOnlyGroww],
        clock: Callable[[], datetime] = ist_now,
        poll_seconds: float = 10.0,
    ):
        self.engine = engine
        self.api_factory = api_factory
        self.clock = clock
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.log: deque[str] = deque(maxlen=200)
        self.status = "stopped"
        self.error: str | None = None
        self._reset_day_state()
        self.config: dict | None = None

    # ------------------------------------------------------------------
    def _reset_day_state(self) -> None:
        self.day: date | None = None
        self.run_id: int | None = None
        self.strategies: list[Strategy] = []
        self.saved: dict[int, int] = {}
        self.processed_until: datetime | None = None
        self.squared_off = False
        self.options = CandleBook()
        self.spot_book = CandleBook()
        self.resolver: ContractResolver | None = None
        self.last_poll: datetime | None = None
        self.last_bar: str | None = None

    def _log(self, msg: str) -> None:
        self.log.appendleft(f"{self.clock():%H:%M:%S}  {msg}")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: dict) -> None:
        cfg = self.validate(config)
        with self._lock:
            if self.running:
                raise RuntimeError("Paper engine is already running.")
            self.config = cfg
            self._reset_day_state()
            self.error = None
            self._stop.clear()
            self.status = "starting"
            self._log(
                f"Started: {cfg['timeframe']}m, {cfg['start']}-{cfg['cutoff']}, "
                f"modes {', '.join(cfg['modes'])}"
            )
            self._thread = threading.Thread(
                target=self._loop, name="paper", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        with self._lock:
            if self.run_id is not None and not self.squared_off:
                finish_run(self.engine, self.run_id, "done", "Stopped by user")
            self.status = "stopped"
            self._log("Stopped")

    @staticmethod
    def validate(config: dict) -> dict:
        tf = int(config.get("timeframe", 15))
        if tf not in TIMEFRAMES:
            raise ValueError("Timeframe must be 5, 15 or 30.")
        start = datetime.strptime(config.get("start", "09:30"), "%H:%M").time()
        cutoff = datetime.strptime(config.get("cutoff", "14:30"), "%H:%M").time()
        if not (SESSION_OPEN < start <= cutoff < time(15, 0)):
            raise ValueError(
                "Start must be before cutoff, both between 09:15 and 15:00."
            )
        modes = [m for m in config.get("modes", MODES) if m in MODES] or list(MODES)
        settings = RunSettings.from_dict(config)
        return dict(
            timeframe=tf,
            start=hhmm(start),
            cutoff=hhmm(cutoff),
            modes=modes,
            **settings.as_dict(),
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # keep going; show the error on the page
                self.error = f"{type(exc).__name__}: {exc}"
                self._log(f"Error: {self.error}")
            if self.status == "done":
                break
            self._stop.wait(self.poll_seconds)

    # ------------------------------------------------------------------
    def step(self) -> None:
        """One poll. Safe to call repeatedly; does nothing new if no new data."""
        with self._lock:
            now = self.clock()
            cfg = self.config
            if cfg is None:
                return
            if now.weekday() >= 5:
                self.status = "market_closed"
                return
            if now.time() < time(9, 16):
                self.status = "waiting_open"
                return
            if self.day != now.date():
                self._reset_day_state()
                self._begin_day(now.date())
            api = self.api_factory()
            day = self.day

            # Today's closed 1m candles so far
            rows = fetch_candles(
                api,
                SPOT_SYMBOL,
                SEG_CASH,
                session_open(day),
                min(now, session_close(day)),
            )
            closed = [r for r in rows if r[0] + timedelta(minutes=1) <= now]
            db.upsert_candles(self.engine, SPOT_SYMBOL, closed)
            hist = db.load_candles(
                self.engine,
                SPOT_SYMBOL,
                session_open(day - timedelta(days=WARMUP_DAYS)),
                session_close(day),
            )
            self.spot_book.reset({SPOT_SYMBOL: hist})
            self.last_poll = now
            tf = cfg["timeframe"]
            bars = resample(hist, tf)
            if bars:
                b = bars[-1]
                self.last_bar = f"{b.start:%H:%M}-{b.end:%H:%M} close {b.close:.2f}"
            ready = now - PROCESS_LAG
            sigs = [
                s
                for s in crossovers(bars)
                if s.day == day
                and s.time <= ready
                and (self.processed_until is None or s.time > self.processed_until)
            ]
            for sig in sigs:
                self._refresh_options(api, now, sig)
                for strat in self.strategies:
                    strat.on_signal(sig)
                self.processed_until = sig.time
                arrow = "bullish" if sig.direction > 0 else "bearish"
                self._log(
                    f"{arrow.title()} cross at {sig.time:%H:%M} (NIFTY {sig.spot:.2f})"
                )

            sq = datetime.combine(day, time(15, 0))
            if not self.squared_off and now >= sq + PROCESS_LAG:
                self._refresh_options(api, now, None)
                for strat in self.strategies:
                    strat.square_off(day)
                self.squared_off = True
                self._log("15:00 square-off done")
            self._refresh_open_prices(api, now)
            self._save_new_trades()
            if self.squared_off and now.time() >= STOP_AFTER:
                finish_run(self.engine, self.run_id, "done", "Day complete")
                self.status = "done"
                self._log("Day complete. Engine stopped.")
            else:
                self.status = "running"

    def _begin_day(self, day: date) -> None:
        cfg = self.config
        api = self.api_factory()
        self.day = day
        # Warm-up spot history for the EMAs
        frm = day - timedelta(days=WARMUP_DAYS)
        have = db.covered_days(self.engine, SPOT_SYMBOL, frm, day)
        if any(d not in have for d in weekdays(frm, day - timedelta(days=1))):
            rows = fetch_candles(
                api,
                SPOT_SYMBOL,
                SEG_CASH,
                session_open(frm),
                session_close(day - timedelta(days=1)),
            )
            db.upsert_candles(self.engine, SPOT_SYMBOL, rows)
            counts: dict[date, int] = {}
            for r in rows:
                counts[r[0].date()] = counts.get(r[0].date(), 0) + 1
            db.mark_covered(
                self.engine,
                SPOT_SYMBOL,
                {d: counts.get(d, 0) for d in weekdays(frm, day - timedelta(days=1))},
            )
        # Expiries for this month and next, and the contract list of the nearest
        for d in (day, (day.replace(day=1) + timedelta(days=32))):
            db.save_expiries(
                self.engine,
                UNDERLYING,
                d.year,
                d.month,
                fetch_expiries(api, d.year, d.month),
            )
        exps = db.load_expiries(self.engine, UNDERLYING)
        exp = nearest_expiry(day, exps)
        listed: set[date] = set()
        if exp:
            try:
                rows = fetch_contracts(api, exp)
            except Exception:
                rows = []
            if rows:
                db.save_contracts(self.engine, UNDERLYING, exp, rows)
                listed = {exp}
        self.resolver = ContractResolver(
            exps, db.load_contracts(self.engine, listed), listed
        )
        # One paper run per day + config; a restart replays the day into it.
        self.run_id = self._find_or_create_run(day, cfg)
        with self.engine.begin() as conn:
            db.delete_run_rows(conn, self.run_id)
        slip = Slippage(cfg["slippage_pct"], cfg["slippage_points"])
        start = datetime.strptime(cfg["start"], "%H:%M").time()
        cutoff = datetime.strptime(cfg["cutoff"], "%H:%M").time()
        self.strategies = [
            Strategy(
                StrategyConfig(
                    cfg["timeframe"],
                    start,
                    cutoff,
                    m,
                    cfg["lot_size"],
                    cfg["lots"],
                    slippage=slip,
                ),
                self.options,
                self.resolver,
                self.spot_book,
            )
            for m in cfg["modes"]
        ]
        self.saved = {i: 0 for i in range(len(self.strategies))}
        self._log(f"Trading day {day}; nearest expiry {exp}")

    def _find_or_create_run(self, day: date, cfg: dict) -> int:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(db.runs.c.id, db.runs.c.params).where(
                    and_(db.runs.c.kind == "paper", db.runs.c.date_from == day)
                )
            ).all()
        for rid, params in rows:
            if params == cfg:

                with self.engine.begin() as conn:
                    conn.execute(
                        update(db.runs)
                        .where(db.runs.c.id == rid)
                        .values(status="running", message=None)
                    )
                return rid
        rid = create_run(
            self.engine, day, day, RunSettings.from_dict(cfg), kind="paper"
        )

        with self.engine.begin() as conn:
            conn.execute(update(db.runs).where(db.runs.c.id == rid).values(params=cfg))
        return rid

    def _symbols_for(self, sig) -> set[str]:
        syms = {s.position.symbol for s in self.strategies if s.position}
        if sig is not None:
            for m in self.config["modes"]:
                leg = pick_leg(m, sig.direction, sig.spot)
                r = self.resolver(sig.day, leg.opt_type, leg.strike)
                if r:
                    syms.add(r[0])
        return syms

    def _refresh_options(self, api, now: datetime, sig) -> None:
        for sym in self._symbols_for(sig):
            rows = fetch_candles(
                api,
                sym,
                SEG_FNO,
                session_open(self.day),
                min(now, session_close(self.day)),
            )
            closed = [r for r in rows if r[0] + timedelta(minutes=1) <= now]
            db.upsert_candles(self.engine, sym, closed)
            self.options.add(sym, [Candle(*r[:6]) for r in closed])

    def _refresh_open_prices(self, api, now: datetime) -> None:
        if any(s.position for s in self.strategies):
            self._refresh_options(api, now, None)

    def _save_new_trades(self) -> None:
        rows = []
        for i, s in enumerate(self.strategies):
            new = s.trades[self.saved[i] :]
            rows.extend(trade_row(self.run_id, t) for t in new)
            for t in new:
                self._log(
                    f"{t.mode} closed {t.symbol}: net Rs {t.net:,.2f} ({t.exit_reason})"
                )
            self.saved[i] = len(s.trades)
        if rows:
            with self.engine.begin() as conn:
                conn.execute(insert(db.trades), rows)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            positions = []
            for s in self.strategies:
                p = s.position
                if p is None:
                    continue
                last = self.options.latest_at_or_before(p.symbol, self.clock())
                ltp = last.close if last else None
                mtm = None
                if ltp is not None:
                    sign = 1 if p.side == "BUY" else -1
                    mtm = round((ltp - p.entry_raw) * p.qty * sign, 2)
                positions.append(
                    dict(
                        mode=p.mode,
                        symbol=p.symbol,
                        side=p.side,
                        entry_time=f"{p.entry_time:%H:%M}",
                        entry=p.entry_raw,
                        ltp=ltp,
                        mtm_gross=mtm,
                    )
                )
            closed = [t for s in self.strategies for t in s.trades]
            return dict(
                status=self.status,
                running=self.running,
                error=self.error,
                config=self.config,
                day=self.day.isoformat() if self.day else None,
                run_id=self.run_id,
                last_poll=self.last_poll.isoformat() if self.last_poll else None,
                last_bar=self.last_bar,
                positions=positions,
                closed_trades=len(closed),
                closed_net=round(sum(t.net for t in closed), 2),
                log=list(self.log)[:60],
                live_trading_enabled=LIVE_TRADING_ENABLED,
            )
