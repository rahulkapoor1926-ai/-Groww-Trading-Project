"""Free Hugging Face import path, tested on local parquet files that copy the
real datasets' column layouts (no network)."""

from datetime import date, datetime, time, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from app import db, free_data
from app.backtest import CUTOFFS, STARTS, RunSettings, run_backtest
from app.groww_data import ReadOnlyGroww
from app.strategy.bars import TIMEFRAMES
from app.strategy.contracts import MODES

from .fakes import FakeGroww, option_price, spot_price, trading_day, tuesdays

FROM, TO = date(2025, 9, 29), date(2025, 10, 7)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def minutes(d: date):
    t = datetime.combine(d, time(9, 15))
    while t.time() < time(15, 30):
        yield t
        t += timedelta(minutes=1)


def days(lo: date, hi: date):
    d = lo
    while d <= hi:
        if trading_day(d):
            yield d
        d += timedelta(days=1)


def write_spot(path, lo, hi, skip_days=(), end_labelled=False):
    ts, o, h, lw, c = [], [], [], [], []
    for d in days(lo, hi):
        if d in skip_days:
            continue
        for t in minutes(d):
            p = spot_price(t)
            stamp = t + timedelta(minutes=1) if end_labelled else t
            ts.append(stamp.replace(tzinfo=IST).astimezone(UTC))  # tz-aware, UTC stored
            o.append(p)
            h.append(p + 2)
            lw.append(p - 2)
            c.append(p + 0.5)
    n = len(ts)
    table = pa.table(
        {
            "timestamp": pa.array(ts, pa.timestamp("ns", tz="Asia/Kolkata")),
            "open": o,
            "high": h,
            "low": lw,
            "close": c,
            "volume": [0] * n,
            "open_interest": [None] * n,
            "trading_day": [t.astimezone(IST).date() for t in ts],
            "symbol": ["NIFTY"] * n,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def expiries_for(lo, hi):
    out, d = [], date(lo.year, lo.month, 1)
    while d <= hi + timedelta(days=40):
        out += tuesdays(d.year, d.month)
        d = (d + timedelta(days=32)).replace(day=1)
    return sorted(e for e in out if e >= lo)


def write_options(path, lo, hi, strikes, bad_rows=0, drop=None):
    from app.strategy.contracts import nearest_expiry, option_symbol

    exps = expiries_for(lo, hi)
    cols = {
        k: []
        for k in (
            "date",
            "timestamp",
            "underlying",
            "expiry",
            "strike",
            "option_type",
            "exercise_style",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "oi",
            "settle_price",
            "source",
            "granularity",
        )
    }
    bad = 0
    for d in days(lo, hi):
        exp = nearest_expiry(d, exps)
        for k in strikes:
            for typ in ("CE", "PE"):
                sym = option_symbol(exp, k, typ)
                for t in minutes(d):
                    if drop and drop(d, k, typ, t):
                        continue
                    p = option_price(sym, t)
                    hi_ = p + 1
                    if bad < bad_rows:  # high below low: must be dropped
                        hi_, bad = p - 5, bad + 1
                    for key, v in (
                        ("date", d),
                        ("timestamp", t),
                        ("underlying", "NIFTY"),
                        ("expiry", exp),
                        ("strike", float(k)),
                        ("option_type", typ),
                        ("exercise_style", "European"),
                        ("open", p),
                        ("high", hi_),
                        ("low", p - 1),
                        ("close", p),
                        ("volume", 100.0),
                        ("oi", None),
                        ("settle_price", None),
                        ("source", "upstox"),
                        ("granularity", "1minute"),
                    ):
                        cols[key].append(v)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            **cols,
            "oi": pa.array(cols["oi"], pa.float64()),
            "settle_price": pa.array(cols["settle_price"], pa.float64()),
        }
    )
    pq.write_table(table, path)
    return len(cols["date"]) - bad


def local_root(tmp_path, monkeypatch):
    root = tmp_path / "hf"
    monkeypatch.setenv("FREE_DATA_LOCAL", str(root))
    monkeypatch.setenv("FREE_DATA_DIR", str(tmp_path / "cache"))
    return root


def spot_file(root):
    return root / "thetrademarkk__india-index-options-1m" / "index" / "NIFTY.parquet"


def opt_file(root, year=2025):
    return (
        root
        / "rissin__nse-options-intraday"
        / "upstox_intraday"
        / "NIFTY"
        / f"NIFTY_{year}.parquet"
    )


FULL_STRIKES = range(23800, 25301, 50)


def test_full_import_runs_all_189_combinations(engine, tmp_path, monkeypatch):
    root = local_root(tmp_path, monkeypatch)
    write_spot(spot_file(root), FROM - timedelta(days=15), TO)
    write_options(opt_file(root), FROM, TO, FULL_STRIKES)

    report = free_data.run_free_backtest(engine, FROM, TO, RunSettings())
    q = report["summary"]
    assert q["trading_days"] == 6  # 7 weekdays minus the 2 Oct holiday
    assert report["spot"]["weekdays_without_data"] == ["2025-10-02"]
    assert q["strike_not_in_dataset"] == 0 and q["no_candles_that_day"] == 0
    assert q["incomplete_spot_days"] == 0 and q["spot_rows_dropped"] == 0
    with engine.connect() as conn:
        run = conn.execute(
            select(db.runs).where(db.runs.c.id == report["run_id"])
        ).one()
        combos = conn.execute(
            select(
                db.trades.c.timeframe,
                db.trades.c.start,
                db.trades.c.cutoff,
                db.trades.c.mode,
            ).distinct()
        ).all()
    assert run.status == "done"
    assert run.params["data_source"] == "huggingface_free"
    assert (
        len(combos) == len(TIMEFRAMES) * len(STARTS) * len(CUTOFFS) * len(MODES) == 189
    )


def test_same_trades_as_groww_path(engine, tmp_path, monkeypatch):
    """Same prices in, same trades/P&L/charges/slippage out, whichever source."""
    root = local_root(tmp_path, monkeypatch)
    write_spot(spot_file(root), FROM - timedelta(days=15), TO)
    write_options(opt_file(root), FROM, TO, FULL_STRIKES)
    free_id = free_data.run_free_backtest(engine, FROM, TO, RunSettings())["run_id"]

    groww_engine = db.make_engine(f"sqlite:///{tmp_path / 'groww.db'}")
    db.init_db(groww_engine)
    api = ReadOnlyGroww(FakeGroww(), min_interval=0, sleep=lambda s: None)
    groww_id = run_backtest(
        groww_engine, api, FROM, TO, RunSettings(), today=date(2025, 11, 1)
    )

    cols = [
        db.trades.c[k]
        for k in (
            "timeframe",
            "start",
            "cutoff",
            "mode",
            "symbol",
            "entry_time",
            "entry_fill",
            "exit_time",
            "exit_fill",
            "net",
        )
    ]

    def trades(eng, rid):
        with eng.connect() as conn:
            rows = conn.execute(select(*cols).where(db.trades.c.run_id == rid)).all()
        return sorted((*r[:-1], round(r[-1], 2)) for r in rows)

    a, b = trades(engine, free_id), trades(groww_engine, groww_id)
    assert len(a) > 100
    assert a == b


def test_never_fabricates_and_flags_gaps(engine, tmp_path, monkeypatch):
    root = local_root(tmp_path, monkeypatch)
    missing_day = date(2025, 10, 6)
    write_spot(spot_file(root), FROM - timedelta(days=15), TO, skip_days={missing_day})
    # narrow strike range -> some ITM legs are not in the dataset;
    # one contract loses its afternoon; 3 rows have high < low
    stored = write_options(
        opt_file(root),
        FROM,
        TO,
        range(24350, 24701, 50),
        bad_rows=3,
        drop=lambda d, k, typ, t: k == 24500 and typ == "CE" and t.hour >= 12,
    )
    report = free_data.run_free_backtest(engine, FROM, TO, RunSettings())
    q = report["summary"]
    assert "2025-10-06" in report["spot"]["weekdays_without_data"]
    assert q["strike_not_in_dataset"] > 0
    assert q["option_rows_dropped"] <= 3
    with engine.connect() as conn:
        n_opt = conn.execute(
            select(func.count())
            .select_from(db.candles)
            .where(db.candles.c.symbol != "NSE-NIFTY")
        ).scalar()
        n_spot_day = conn.execute(
            select(func.count())
            .select_from(db.candles)
            .where(
                db.candles.c.symbol == "NSE-NIFTY",
                db.candles.c.ts >= datetime(2025, 10, 6),
                db.candles.c.ts < datetime(2025, 10, 7),
            )
        ).scalar()
        events = {r[0] for r in conn.execute(select(db.events.c.kind).distinct())}
    assert n_opt <= stored  # only rows that exist in the file
    assert n_spot_day == 0  # missing day is not filled in
    assert "no_contract" in events  # unavailable strikes are reported, not priced


def test_end_labelled_candles_are_detected():
    t = [datetime(2025, 1, 2, 9, 16) + timedelta(minutes=i) for i in range(375)]
    assert free_data.detect_end_labelled(t)
    assert not free_data.detect_end_labelled([x - timedelta(minutes=1) for x in t])


def test_end_labelled_spot_file_is_shifted(engine, tmp_path, monkeypatch):
    root = local_root(tmp_path, monkeypatch)
    write_spot(spot_file(root), FROM - timedelta(days=15), TO, end_labelled=True)
    rows, stats = free_data.read_spot(spot_file(root), FROM, TO)
    assert stats["end_labelled_shifted"]
    assert rows[0][0] == datetime(2025, 9, 29, 9, 15)
    assert all(r[0].time() < time(15, 30) for r in rows)


def test_reports_dates_outside_dataset(engine, tmp_path, monkeypatch):
    root = local_root(tmp_path, monkeypatch)
    write_spot(spot_file(root), FROM - timedelta(days=15), TO)
    write_options(opt_file(root), FROM, TO, FULL_STRIKES)
    with pytest.raises(free_data.FreeDataError, match="no candles"):
        free_data.import_free_data(engine, date(2025, 12, 1), date(2025, 12, 5))


def test_missing_option_year_is_reported(engine, tmp_path, monkeypatch):
    root = local_root(tmp_path, monkeypatch)
    write_spot(spot_file(root), date(2023, 12, 1), date(2024, 1, 10))
    with pytest.raises(free_data.FreeDataError, match="No NIFTY option files"):
        free_data.import_free_data(engine, date(2024, 1, 2), date(2024, 1, 5))


def test_ohlc_validation():
    assert free_data.ohlc_ok(10, 11, 9, 10.5)
    assert not free_data.ohlc_ok(10, 9, 11, 10)
    assert not free_data.ohlc_ok(0, 0, 0, 0)
    assert not free_data.ohlc_ok(None, 1, 1, 1)
    assert not free_data.ohlc_ok(float("nan"), 11, 9, 10)


def test_dashboard_free_import_needs_no_groww_login(tmp_path, monkeypatch):
    from app import create_app
    from app.groww_client import GrowwSession

    root = local_root(tmp_path, monkeypatch)
    write_spot(spot_file(root), FROM - timedelta(days=15), TO)
    write_options(opt_file(root), FROM, TO, FULL_STRIKES)

    def no_groww():
        raise AssertionError("Groww must not be used for the free import")

    app = create_app(
        GrowwSession(api_key=None, totp_secret=None),
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        api_factory=no_groww,
    )
    client = app.test_client()
    r = client.post(
        "/api/free-data", json={"from": FROM.isoformat(), "to": TO.isoformat()}
    )
    assert r.status_code == 200, r.get_json()
    app.extensions["jobs"].join(120)
    st = client.get("/api/job").get_json()
    assert st["status"] == "done", st
    assert st["result"]["data_quality"]["trading_days"] == 6
    rid = st["result"]["run_id"]
    assert client.get(f"/api/summary?run={rid}").status_code == 200
    rep = client.get("/api/free-data/report").get_json()
    assert rep["run_id"] == rid
