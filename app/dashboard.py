"""Backtest dashboard, data page and paper-trading page + their JSON APIs."""

from __future__ import annotations

import csv
import io
import threading
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, current_app, jsonify, render_template, request
from sqlalchemy import select

from . import db
from .backtest import (
    CUTOFFS,
    STARTS,
    WARMUP_DAYS,
    RunSettings,
    hhmm,
    ist_today,
    latest_run_id,
    run_backtest,
)
from .groww_data import SPOT_SYMBOL
from .strategy.bars import TIMEFRAMES, resample, session_close, session_open
from .strategy.contracts import MODE_LABELS, MODES
from .strategy.indicators import ema
from .strategy.metrics import TradePnl, compute

bp = Blueprint("dash", __name__)

OUTCOMES = ("all", "win", "loss")
CROSSES = ("all", "1", "2", "3", "4+")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def engine():
    return current_app.extensions["db"]


def db_error() -> str | None:
    return current_app.extensions.get("db_error")


@bp.before_app_request
def _retry_db() -> None:
    """Postgres may still be starting when the app boots: retry every 5s."""
    ext = current_app.extensions
    if not ext.get("db_error"):
        return
    now = datetime.now().timestamp()
    if now - ext.get("db_retry_at", 0) < 5:
        return
    ext["db_retry_at"] = now
    try:
        db.init_db(ext["db"])
        ext["db_error"] = None
    except Exception:
        pass


class LRU:
    def __init__(self, size: int = 256):
        self.size = size
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key, value) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


summary_cache = LRU(256)
trade_cache = LRU(512)


def _iso(v):
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _row(r) -> dict:
    return {k: _iso(v) for k, v in r._mapping.items()}


def _get_run(run_id: int | None):
    with engine().connect() as conn:
        if run_id is None:
            run_id = latest_run_id(engine())
            if run_id is None:
                return None
        r = conn.execute(select(db.runs).where(db.runs.c.id == run_id)).first()
        return r


def _api_error(msg: str, status: int = 400):
    return jsonify({"error": msg}), status


def _parse_filters(args, run) -> dict:
    params = run.params or {}
    is_paper = run.kind == "paper"
    tf = int(args.get("tf", params.get("timeframe", 15)))
    start = args.get("start", params.get("start", "09:30"))
    cutoff = args.get("cutoff", params.get("cutoff", "14:30"))
    if is_paper:  # a paper run only has its own settings
        tf, start, cutoff = params["timeframe"], params["start"], params["cutoff"]
    mode = args.get("mode", MODES[0])
    outcome = args.get("outcome", "all")
    cross = args.get("cross", "all")
    if tf not in TIMEFRAMES:
        raise ValueError("bad timeframe")
    if mode not in MODES:
        raise ValueError("bad mode")
    if outcome not in OUTCOMES or cross not in CROSSES:
        raise ValueError("bad filter")
    if not is_paper and (
        start not in {hhmm(t) for t in STARTS}
        or cutoff not in {hhmm(t) for t in CUTOFFS}
    ):
        raise ValueError("bad time window")
    return dict(
        tf=tf, start=start, cutoff=cutoff, mode=mode, outcome=outcome, cross=cross
    )


def _cross_ok(n: int, cross: str) -> bool:
    if cross == "all":
        return True
    if cross == "4+":
        return n >= 4
    return n == int(cross)


def _outcome_ok(net: float, outcome: str) -> bool:
    return (
        outcome == "all"
        or (outcome == "win" and net > 0)
        or (outcome == "loss" and net < 0)
    )


def _load_trades(
    run_id: int, tf: int, start: str, cutoff: str, mode: str | None = None
):
    t = db.trades
    q = select(t).where(
        t.c.run_id == run_id,
        t.c.timeframe == tf,
        t.c.start == start,
        t.c.cutoff == cutoff,
    )
    if mode:
        q = q.where(t.c.mode == mode)
    q = q.order_by(t.c.entry_time, t.c.id)
    with engine().connect() as conn:
        return conn.execute(q).all()


def _pnl(r) -> TradePnl:
    return TradePnl(net=r.net, gross=r.gross, friction=r.friction)


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
@bp.get("/backtest")
def backtest_page():
    return render_template(
        "backtest.html",
        modes=MODES,
        mode_labels=MODE_LABELS,
        timeframes=TIMEFRAMES,
        starts=[hhmm(t) for t in STARTS],
        cutoffs=[hhmm(t) for t in CUTOFFS],
        db_error=db_error(),
    )


@bp.get("/data")
def data_page():
    today = ist_today()
    return render_template(
        "data.html",
        db_error=db_error(),
        default_from=(today - timedelta(days=60)).isoformat(),
        default_to=(today - timedelta(days=1)).isoformat(),
        settings=RunSettings(),
    )


@bp.get("/paper")
def paper_page():
    return render_template(
        "paper.html",
        db_error=db_error(),
        modes=MODES,
        mode_labels=MODE_LABELS,
        timeframes=TIMEFRAMES,
        starts=[hhmm(t) for t in STARTS],
        cutoffs=[hhmm(t) for t in CUTOFFS],
    )


# --------------------------------------------------------------------------
# APIs
# --------------------------------------------------------------------------
@bp.get("/api/runs")
def api_runs():
    if db_error():
        return _api_error(db_error(), 503)
    with engine().connect() as conn:
        rows = conn.execute(
            select(db.runs).order_by(db.runs.c.id.desc()).limit(50)
        ).all()
    return jsonify([_row(r) for r in rows])


@bp.get("/api/summary")
def api_summary():
    if db_error():
        return _api_error(db_error(), 503)
    run_id = request.args.get("run", type=int)
    run = _get_run(run_id)
    if run is None:
        return jsonify({"empty": True, "reason": "no_runs"})
    try:
        f = _parse_filters(request.args, run)
    except (ValueError, KeyError) as exc:
        return _api_error(f"Invalid filter: {exc}")
    key = (run.id, tuple(sorted(f.items())))
    cacheable = run.status == "done"
    if cacheable and (hit := summary_cache.get(key)) is not None:
        return jsonify(hit)

    capital = float((run.params or {}).get("capital", 200_000))
    all_rows = _load_trades(run.id, f["tf"], f["start"], f["cutoff"])
    by_mode = defaultdict(list)
    for r in all_rows:
        if _cross_ok(r.cross_no, f["cross"]) and _outcome_ok(r.net, f["outcome"]):
            by_mode[r.mode].append(r)
    rows = by_mode.get(f["mode"], [])
    metrics = compute([_pnl(r) for r in rows], capital)
    modes = {}
    for m in MODES:
        mm = compute([_pnl(r) for r in by_mode.get(m, [])], capital)
        mm.pop("equity")
        modes[m] = mm

    trades = [
        dict(
            id=r.id,
            day=r.day.isoformat(),
            entry=f"{r.entry_time:%H:%M}",
            exit=f"{r.exit_time:%H:%M}",
            direction=r.direction,
            side=r.side,
            strike=r.strike,
            opt_type=r.opt_type,
            net=r.net,
            gross=r.gross,
            whipsaw=r.whipsaw,
            exit_reason=r.exit_reason,
            cross_no=r.cross_no,
        )
        for r in rows
    ]
    payload = dict(
        run=_row(run),
        filters=f,
        metrics=metrics,
        modes=modes,
        trades=trades,
        mode_labels=MODE_LABELS,
        days=_days(run.id, f, rows),
    )
    if cacheable:
        summary_cache.put(key, payload)
    return jsonify(payload)


def _days(run_id: int, f: dict, rows) -> list[dict]:
    """Per-day log including no-trade days and skipped entries."""
    with engine().connect() as conn:
        dayrows = conn.execute(
            select(db.days).where(db.days.c.run_id == run_id).order_by(db.days.c.day)
        ).all()
        ev = conn.execute(
            select(db.events).where(
                db.events.c.run_id == run_id,
                db.events.c.timeframe == f["tf"],
                db.events.c.start == f["start"],
                db.events.c.cutoff == f["cutoff"],
                db.events.c.mode == f["mode"],
            )
        ).all()
    per_day = defaultdict(lambda: {"trades": 0, "net": 0.0})
    for r in rows:
        d = per_day[r.day]
        d["trades"] += 1
        d["net"] = round(d["net"] + r.net, 2)
    events = defaultdict(list)
    for e in ev:
        events[e.day].append(f"{e.time:%H:%M} {e.detail}")
    out = []
    for d in dayrows:
        info = per_day.get(d.day, {"trades": 0, "net": 0.0})
        if info["trades"]:
            status = "traded"
        elif events.get(d.day):
            status = "skipped"
        elif d.status == "incomplete":
            status = "incomplete_data"
        else:
            status = "no_signal"
        out.append(
            dict(
                day=d.day.isoformat(),
                minutes=d.minutes,
                data=d.status,
                status=status,
                trades=info["trades"],
                net=info["net"],
                events=events.get(d.day, []),
            )
        )
    return out


@bp.get("/api/trade/<int:trade_id>")
def api_trade(trade_id: int):
    if db_error():
        return _api_error(db_error(), 503)
    hit = trade_cache.get(trade_id)
    if hit is None:
        with engine().connect() as conn:
            r = conn.execute(
                select(db.trades).where(db.trades.c.id == trade_id)
            ).first()
        if r is None:
            return _api_error("Trade not found", 404)
        hit = dict(trade=_row(r), chart=_chart(r))
        trade_cache.put(trade_id, hit)
    resp = jsonify(hit)
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


def _epoch(ts: datetime) -> int:
    # Lightweight Charts shows times in UTC; passing IST wall-clock as if it
    # were UTC makes the axis read in IST.
    return int((ts - datetime(1970, 1, 1)).total_seconds())


def _chart(r) -> dict:
    day = r.day
    tf = r.timeframe
    spot = db.load_candles(
        engine(),
        SPOT_SYMBOL,
        session_open(day - timedelta(days=WARMUP_DAYS)),
        session_close(day),
    )
    bars = resample(spot, tf)
    closes = [b.close for b in bars]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    sb, s9, s21 = [], [], []
    for b, a, c in zip(bars, e9, e21, strict=True):
        if b.day != day:
            continue
        t = _epoch(b.start)
        sb.append(dict(time=t, open=b.open, high=b.high, low=b.low, close=b.close))
        if a is not None:
            s9.append(dict(time=t, value=round(a, 2)))
        if c is not None:
            s21.append(dict(time=t, value=round(c, 2)))
    opt = db.load_candles(engine(), r.symbol, session_open(day), session_close(day))
    ob = [
        dict(time=_epoch(c.ts), open=c.open, high=c.high, low=c.low, close=c.close)
        for c in opt
    ]

    def bar_of(ts: datetime | None) -> int | None:
        # Marker sits on the signal bar (the one whose close triggered it)
        if ts is None:
            return None
        for b in bars:
            if b.end == ts:
                return _epoch(b.start)
        for b in reversed(bars):
            if b.start <= ts:
                return _epoch(b.start)
        return None

    return dict(
        spot=sb,
        ema9=s9,
        ema21=s21,
        option=ob,
        entry_bar=bar_of(r.entry_signal_time or r.entry_time),
        exit_bar=bar_of(r.exit_signal_time or r.exit_time),
        entry_minute=_epoch(r.entry_time),
        exit_minute=_epoch(r.exit_time),
    )


@bp.get("/api/data/summary")
def api_data_summary():
    if db_error():
        return _api_error(db_error(), 503)
    s = db.data_summary(engine(), SPOT_SYMBOL)
    return jsonify({k: _iso(v) for k, v in s.items()})


@bp.post("/api/backtest")
def api_start_backtest():
    if db_error():
        return _api_error(db_error(), 503)
    body = request.get_json(silent=True) or {}
    try:
        date_from = date.fromisoformat(body["from"])
        date_to = date.fromisoformat(body["to"])
        settings = RunSettings.from_dict(body)
    except (KeyError, ValueError) as exc:
        return _api_error(f"Please give valid dates and settings ({exc}).")
    if date_from > date_to:
        return _api_error("Start date must be on or before end date.")
    if date_to - date_from > timedelta(days=800):
        return _api_error("Please pick at most about two years per run.")
    if (
        settings.lots < 1
        or settings.lot_size < 1
        or settings.slippage_pct < 0
        or settings.slippage_points < 0
        or settings.capital <= 0
    ):
        return _api_error(
            "Lots, lot size and capital must be positive; slippage can't be negative."
        )
    download_first = bool(body.get("download", True))
    app = current_app._get_current_object()
    eng = engine()

    def job(progress):
        api = app.extensions["groww_api"]() if download_first else None
        run_id = run_backtest(eng, api, date_from, date_to, settings, progress)
        summary_cache.clear()
        return {"run_id": run_id}

    if not app.extensions["jobs"].start("backtest", job):
        return _api_error("A job is already running. Wait for it to finish.", 409)
    return jsonify({"started": True})


@bp.post("/api/free-data")
def api_free_data():
    """Import free Hugging Face 1m data (no Groww login), then run all combos."""
    from . import free_data

    if db_error():
        return _api_error(db_error(), 503)
    body = request.get_json(silent=True) or {}
    try:
        date_from = date.fromisoformat(body["from"])
        date_to = date.fromisoformat(body["to"])
        settings = RunSettings.from_dict(body)
    except (KeyError, ValueError) as exc:
        return _api_error(f"Please give valid dates and settings ({exc}).")
    if date_from > date_to:
        return _api_error("Start date must be on or before end date.")
    if date_to - date_from > timedelta(days=400):
        return _api_error("Please pick at most about one year per free-data import.")
    app = current_app._get_current_object()
    eng = engine()

    def job(progress):
        report = free_data.run_free_backtest(
            eng, date_from, date_to, settings, progress
        )
        free_data.save_report(report)
        summary_cache.clear()
        return {"run_id": report["run_id"], "data_quality": report["summary"]}

    if not app.extensions["jobs"].start("free_data", job):
        return _api_error("A job is already running. Wait for it to finish.", 409)
    return jsonify({"started": True})


@bp.get("/api/free-data/report")
def api_free_data_report():
    from . import free_data

    p = free_data.cache_dir() / "last_import_report.json"
    if not p.exists():
        return _api_error("No free-data import has run yet.", 404)
    return current_app.response_class(p.read_text(), mimetype="application/json")


@bp.get("/api/job")
def api_job():
    st = dict(current_app.extensions["jobs"].state)
    st.pop("trace", None)
    return jsonify(st)


@bp.get("/export/run/<int:run_id>.csv")
def export_run(run_id: int):
    if db_error():
        return _api_error(db_error(), 503)
    with engine().connect() as conn:
        rows = conn.execute(
            select(db.trades)
            .where(db.trades.c.run_id == run_id)
            .order_by(db.trades.c.id)
        ).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = [c.name for c in db.trades.columns]
    w.writerow(cols)
    for r in rows:
        w.writerow([_iso(getattr(r, c)) for c in cols])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=run-{run_id}-trades.csv"
        },
    )


# --- paper engine ---------------------------------------------------------
@bp.get("/api/paper")
def api_paper_state():
    return jsonify(current_app.extensions["paper"].snapshot())


@bp.post("/api/paper/start")
def api_paper_start():
    if db_error():
        return _api_error(db_error(), 503)
    try:
        current_app.extensions["paper"].start(request.get_json(silent=True) or {})
    except (ValueError, RuntimeError) as exc:
        return _api_error(str(exc))
    return jsonify(current_app.extensions["paper"].snapshot())


@bp.post("/api/paper/stop")
def api_paper_stop():
    current_app.extensions["paper"].stop()
    return jsonify(current_app.extensions["paper"].snapshot())
