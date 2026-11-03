"""Import free public 1-minute NIFTY data from Hugging Face (no Groww login).

Two public datasets are combined, because neither has everything:

* NIFTY index (spot) 1-minute candles: ``thetrademarkk/india-index-options-1m``,
  file ``index/NIFTY.parquet``. The strategy's EMA signals need these.
* NIFTY option 1-minute candles: ``rissin/nse-options-intraday``, files
  ``upstox_intraday/NIFTY/NIFTY_<year>.parquet`` (Oct 2024 onwards). This
  dataset has no index candles.

The importer only copies rows that exist in those files into the same tables
the Groww downloader uses. It never creates, fills or interpolates a candle or
price. Rows that fail basic checks are dropped and counted, and everything
missing is listed in a data-quality report that is saved with the run.

Run from the Data page, or in a terminal:
    python -m app.free_data --from 2026-09-01 --to 2026-10-31
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.engine import Engine

from . import db
from .backtest import (
    CUTOFFS,
    STARTS,
    WARMUP_DAYS,
    RunSettings,
    SignalSet,
    run_backtest,
    weekdays,
)
from .groww_data import SPOT_SYMBOL, UNDERLYING
from .strategy.bars import TIMEFRAMES, Candle, in_session
from .strategy.contracts import MODES, nearest_expiry, option_symbol, pick_leg

IST = timezone(timedelta(hours=5, minutes=30))
SPOT_REPO = "thetrademarkk/india-index-options-1m"
SPOT_PATH = "index/NIFTY.parquet"
OPT_REPO = "rissin/nse-options-intraday"
OPT_PATH = "upstox_intraday/NIFTY/NIFTY_{year}.parquet"
FULL_DAY = 375
SPARSE_BELOW = 0.5  # an option contract-day with < 50% of minutes is "sparse"
SOURCE_TAG = "huggingface_free"

Progress = Callable[[str, int, int, str], None]


def _noop(stage: str, done: int, total: int, msg: str) -> None:
    pass


class FreeDataError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
def hf_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def cache_dir() -> Path:
    d = Path(os.getenv("FREE_DATA_DIR", "data/free_cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch(repo: str, path: str, progress: Progress = _noop) -> Path:
    """Download one file into the cache (once). Set FREE_DATA_LOCAL to a folder
    that mirrors the dataset paths to skip downloading entirely."""
    local_root = os.getenv("FREE_DATA_LOCAL")
    if local_root:
        p = Path(local_root) / repo.replace("/", "__") / path
        if not p.exists():
            raise FreeDataError(f"Local file not found: {p}")
        return p
    import requests

    dest = cache_dir() / repo.replace("/", "__") / path
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {}
    if os.getenv("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
    url = hf_url(repo, path)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        if r.status_code == 404:
            raise FreeDataError(f"{repo}/{path} does not exist (HTTP 404).")
        if r.status_code >= 400:
            raise FreeDataError(f"Download of {url} failed: HTTP {r.status_code}")
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(
                    "download", done >> 20, total >> 20, f"Downloading {path} (MB)"
                )
    tmp.rename(dest)
    return dest


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------
def to_ist(v) -> datetime | None:
    """Any timestamp form in the files -> naive IST datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(IST).replace(tzinfo=None) if v.tzinfo else v
    if isinstance(v, int | float):
        secs = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(secs, IST).replace(tzinfo=None)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(IST).replace(tzinfo=None) if dt.tzinfo else dt


def to_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def opt_type(v) -> str | None:
    s = str(v).strip().upper()
    return {
        "CE": "CE",
        "C": "CE",
        "CALL": "CE",
        "PE": "PE",
        "P": "PE",
        "PUT": "PE",
    }.get(s)


def ohlc_ok(o, h, lo, c) -> bool:
    try:
        o, h, lo, c = float(o), float(h), float(lo), float(c)
    except (TypeError, ValueError):
        return False
    return 0 < lo <= min(o, c) and max(o, c) <= h


def detect_end_labelled(times: list[datetime]) -> bool:
    """True if candles are stamped with their END minute (09:16..15:30)
    instead of their start (09:15..15:29)."""
    if not times:
        return False
    has_0915 = any(t.time() == time(9, 15) for t in times)
    has_1530 = any(t.time() == time(15, 30) for t in times)
    return has_1530 and not has_0915


def _date_filter(dataset, lo: date, hi: date):
    import pyarrow as pa
    import pyarrow.dataset as ds

    col = "date" if "date" in dataset.schema.names else "timestamp"
    f = ds.field(col).cast(pa.string())
    return (f >= lo.isoformat()) & (f < (hi + timedelta(days=1)).isoformat())


# --------------------------------------------------------------------------
# Spot
# --------------------------------------------------------------------------
def read_spot(path: Path, lo: date, hi: date) -> tuple[list[tuple], dict]:
    """Return clean spot rows (ts, o, h, l, c, v) in [lo, hi] and stats."""
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    names = table.schema.names
    need = {"timestamp", "open", "high", "low", "close"}
    if not need <= set(names):
        raise FreeDataError(f"Spot file is missing columns {sorted(need - set(names))}")
    cols = {n: table.column(n).to_pylist() for n in need | ({"volume"} & set(names))}
    sym = table.column("symbol").to_pylist() if "symbol" in names else None
    stats = {
        "rows_in_file": table.num_rows,
        "dropped_bad_ohlc": 0,
        "dropped_bad_time": 0,
        "duplicates": 0,
        "end_labelled_shifted": False,
    }
    if sym is not None:
        distinct = sorted({str(x) for x in sym})
        stats["symbols_in_file"] = distinct[:5]
        if len(distinct) > 1:
            raise FreeDataError(
                f"Spot file holds several symbols {distinct[:5]}; expected NIFTY only."
            )
    times = [to_ist(t) for t in cols["timestamp"]]
    valid_times = [t for t in times if t]
    if valid_times:
        stats["file_first"] = min(valid_times).isoformat()
        stats["file_last"] = max(valid_times).isoformat()
    shift = detect_end_labelled([t for t in valid_times if lo <= t.date() <= hi])
    if shift:
        stats["end_labelled_shifted"] = True
    out: dict[datetime, tuple] = {}
    for i, t in enumerate(times):
        if t is None:
            stats["dropped_bad_time"] += 1
            continue
        if shift:
            t -= timedelta(minutes=1)
        if not (lo <= t.date() <= hi):
            continue
        if not in_session(t) or t.second:
            stats["dropped_bad_time"] += 1
            continue
        o, h, lw, c = (cols[k][i] for k in ("open", "high", "low", "close"))
        if not ohlc_ok(o, h, lw, c):
            stats["dropped_bad_ohlc"] += 1
            continue
        if t in out:
            stats["duplicates"] += 1
        v = cols.get("volume", [0] * len(times))[i] or 0
        out[t] = (t, float(o), float(h), float(lw), float(c), float(v), None)
    return [out[k] for k in sorted(out)], stats


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------
def open_options(paths: list[Path]):
    import pyarrow.dataset as ds

    dataset = ds.dataset([str(p) for p in paths], format="parquet")
    need = {
        "timestamp",
        "expiry",
        "strike",
        "option_type",
        "open",
        "high",
        "low",
        "close",
    }
    missing = need - set(dataset.schema.names)
    if missing:
        raise FreeDataError(f"Options files are missing columns {sorted(missing)}")
    return dataset


def option_universe(dataset, lo: date, hi: date) -> dict[date, set[tuple[int, str]]]:
    """{expiry: {(strike, CE/PE)}} for contracts that traded in [lo, hi]."""
    t = dataset.to_table(
        columns=["expiry", "strike", "option_type"],
        filter=_date_filter(dataset, lo, hi),
    )
    t = t.group_by(["expiry", "strike", "option_type"]).aggregate([])
    out: dict[date, set] = defaultdict(set)
    for e, k, ot in zip(
        t.column("expiry").to_pylist(),
        t.column("strike").to_pylist(),
        t.column("option_type").to_pylist(),
        strict=True,
    ):
        ed = to_date(e)
        if ed is None or k is None or opt_type(ot) is None:
            continue
        if float(k).is_integer():
            out[ed].add((int(k), opt_type(ot)))
    return dict(out)


def read_options(
    dataset, lo: date, hi: date, wanted: dict[tuple, str]
) -> tuple[dict, dict]:
    """Rows for wanted (day, expiry, strike, type) -> {symbol: [rows]}."""
    import pyarrow as pa
    import pyarrow.dataset as ds

    strikes = sorted({float(k[2]) for k in wanted})
    flt = _date_filter(dataset, lo, hi) & ds.field("strike").cast(pa.float64()).isin(
        strikes
    )
    cols = [
        "timestamp",
        "expiry",
        "strike",
        "option_type",
        "open",
        "high",
        "low",
        "close",
    ]
    if "volume" in dataset.schema.names:
        cols.append("volume")
    stats = {
        "dropped_bad_ohlc": 0,
        "dropped_bad_time": 0,
        "duplicates": 0,
        "end_labelled_shifted": False,
    }
    table = dataset.to_table(columns=cols, filter=flt)
    data = {c: table.column(c).to_pylist() for c in cols}
    times = [to_ist(t) for t in data["timestamp"]]
    shift = detect_end_labelled([t for t in times if t])
    stats["end_labelled_shifted"] = shift
    rows: dict[str, dict[datetime, tuple]] = defaultdict(dict)
    for i, t in enumerate(times):
        if t is None:
            stats["dropped_bad_time"] += 1
            continue
        if shift:
            t -= timedelta(minutes=1)
        key = (
            t.date(),
            to_date(data["expiry"][i]),
            int(float(data["strike"][i])),
            opt_type(data["option_type"][i]),
        )
        sym = wanted.get(key)
        if sym is None:
            continue
        if not in_session(t) or t.second:
            stats["dropped_bad_time"] += 1
            continue
        o, h, lw, c = (data[k][i] for k in ("open", "high", "low", "close"))
        if not ohlc_ok(o, h, lw, c):
            stats["dropped_bad_ohlc"] += 1
            continue
        if t in rows[sym]:
            stats["duplicates"] += 1
        v = (data.get("volume") or [0] * len(times))[i] or 0
        rows[sym][t] = (t, float(o), float(h), float(lw), float(c), float(v), None)
    return {s: [r[k] for k in sorted(r)] for s, r in rows.items()}, stats


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def import_free_data(
    engine: Engine, date_from: date, date_to: date, progress: Progress = _noop
) -> dict:
    """Import spot + needed option candles for [date_from, date_to].

    Returns the data-quality report. Raises FreeDataError if nothing usable."""
    if date_from > date_to:
        raise FreeDataError("Start date must be on or before end date.")
    report: dict = {
        "source": SOURCE_TAG,
        "requested": [date_from.isoformat(), date_to.isoformat()],
        "sources": {
            "spot": hf_url(SPOT_REPO, SPOT_PATH),
            "options": [
                hf_url(OPT_REPO, OPT_PATH.format(year=y))
                for y in range(date_from.year, date_to.year + 1)
            ],
        },
    }

    # 1. Spot
    progress("download", 0, 0, "Downloading NIFTY index candles")
    spot_path = fetch(SPOT_REPO, SPOT_PATH, progress)
    progress("validate", 0, 0, "Checking NIFTY index candles")
    spot_lo = date_from - timedelta(days=WARMUP_DAYS)
    spot_rows, spot_stats = read_spot(spot_path, spot_lo, date_to)
    if not spot_rows:
        raise FreeDataError(
            f"The NIFTY index file has no candles between {spot_lo} and {date_to} "
            f"(file covers {spot_stats.get('file_first', '?')} to "
            f"{spot_stats.get('file_last', '?')})."
        )

    # 2. Options files (one per year)
    progress("download", 0, 0, "Downloading NIFTY option candles")
    opt_paths, missing_years = [], []
    for y in range(date_from.year, date_to.year + 1):
        try:
            opt_paths.append(fetch(OPT_REPO, OPT_PATH.format(year=y), progress))
        except FreeDataError as exc:
            missing_years.append(f"{y}: {exc}")
    report["options_missing_years"] = missing_years
    if not opt_paths:
        raise FreeDataError(
            "No NIFTY option files could be downloaded: " + "; ".join(missing_years)
        )
    progress("validate", 0, 0, "Reading the option contract list")
    dataset = open_options(opt_paths)
    universe = option_universe(dataset, date_from, date_to)
    if not universe:
        raise FreeDataError(
            f"The option files have no NIFTY contracts between "
            f"{date_from} and {date_to}."
        )
    expiry_list = sorted(universe)

    # 3. Which contracts can the 189 combinations touch? (from real spot signals)
    spot_candles = [Candle(*r[:6]) for r in spot_rows]
    days_with_spot = sorted(
        {r[0].date() for r in spot_rows if date_from <= r[0].date()}
    )
    sets = {tf: SignalSet.build(spot_candles, tf) for tf in TIMEFRAMES}
    wanted: dict[tuple, str] = {}
    legs_by_day: dict[date, set] = defaultdict(set)
    no_expiry_days: list[str] = []
    missing_contracts: list[str] = []
    lo_t, hi_t = min(STARTS), max(CUTOFFS)
    for d in days_with_spot:
        exp = nearest_expiry(d, expiry_list)
        if exp is None:
            no_expiry_days.append(d.isoformat())
            continue
        for ss in sets.values():
            for s in ss.by_day.get(d, ()):
                if not (lo_t <= s.time.time() <= hi_t):
                    continue
                for mode in MODES:
                    leg = pick_leg(mode, s.direction, s.spot)
                    sym = option_symbol(exp, leg.strike, leg.opt_type)
                    if (leg.strike, leg.opt_type) not in universe[exp]:
                        missing_contracts.append(f"{d} {sym}")
                        continue
                    wanted[(d, exp, leg.strike, leg.opt_type)] = sym
                    legs_by_day[d].add(sym)

    progress("validate", 0, 0, f"Reading {len(wanted)} option contract-days")
    opt_rows, opt_stats = read_options(dataset, date_from, date_to, wanted)

    # 4. Store (only rows that exist in the files)
    progress("store", 0, 0, "Saving to the database")
    db.upsert_candles(engine, SPOT_SYMBOL, spot_rows)
    counts: dict[date, int] = defaultdict(int)
    for r in spot_rows:
        counts[r[0].date()] += 1
    db.mark_covered(
        engine, SPOT_SYMBOL, {d: counts.get(d, 0) for d in weekdays(spot_lo, date_to)}
    )
    by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for e in expiry_list:
        by_month[(e.year, e.month)].append(e)
    for (y, m), exps in by_month.items():
        db.save_expiries(engine, UNDERLYING, y, m, exps)
    for e, contracts in universe.items():
        db.save_contracts(
            engine,
            UNDERLYING,
            e,
            [
                dict(
                    symbol=option_symbol(e, k, t),
                    underlying=UNDERLYING,
                    expiry=e,
                    strike=float(k),
                    opt_type=t,
                )
                for k, t in sorted(contracts)
            ],
        )
    for sym, rows in opt_rows.items():
        db.upsert_candles(engine, sym, rows)
    per_pair: dict[tuple[str, date], int] = defaultdict(int)
    for sym, rows in opt_rows.items():
        for r in rows:
            per_pair[(sym, r[0].date())] += 1
    for (d, _e, _k, _t), sym in wanted.items():
        db.mark_covered(engine, sym, {d: per_pair.get((sym, d), 0)})

    # 5. Report
    weekday_list = weekdays(date_from, date_to)
    empty_pairs = sorted(
        f"{d} {s}"
        for (d, _e, _k, _t), s in wanted.items()
        if per_pair.get((s, d), 0) == 0
    )
    sparse_pairs = sorted(
        f"{d} {s} ({per_pair[(s, d)]} min)"
        for (d, _e, _k, _t), s in wanted.items()
        if 0 < per_pair.get((s, d), 0) < FULL_DAY * SPARSE_BELOW
    )
    report.update(
        spot={
            **spot_stats,
            "rows_imported": len(spot_rows),
            "trading_days": [d.isoformat() for d in days_with_spot],
            "weekdays_without_data": [
                d.isoformat() for d in weekday_list if d not in counts
            ],
            "incomplete_days": {
                d.isoformat(): counts[d] for d in days_with_spot if counts[d] < FULL_DAY
            },
        },
        options={
            **opt_stats,
            "expiries": [e.isoformat() for e in expiry_list],
            "contract_days_needed": len(wanted) + len(missing_contracts),
            "contract_days_imported": sum(1 for v in per_pair.values() if v),
            "rows_imported": sum(len(v) for v in opt_rows.values()),
            "strike_not_in_dataset": sorted(set(missing_contracts)),
            "no_candles_that_day": empty_pairs,
            "sparse": sparse_pairs,
        },
        days_without_expiry=no_expiry_days,
    )
    if not days_with_spot:
        raise FreeDataError("No NIFTY index candles inside the requested dates.")
    return report


def run_free_backtest(
    engine: Engine,
    date_from: date,
    date_to: date,
    settings: RunSettings | None = None,
    progress: Progress = _noop,
) -> dict:
    """Import, then run all 189 combinations on the stored data (no Groww)."""
    report = import_free_data(engine, date_from, date_to, progress)
    days = report["spot"]["trading_days"]
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    run_id = run_backtest(
        engine, None, first, last, settings or RunSettings(), progress
    )
    with engine.begin() as conn:
        row = conn.execute(db.runs.select().where(db.runs.c.id == run_id)).one()
        params = dict(row.params or {})
        params["data_source"] = SOURCE_TAG
        params["data_quality"] = summarize(report)
        conn.execute(
            update(db.runs).where(db.runs.c.id == run_id).values(params=params)
        )
    report["run_id"] = run_id
    report["summary"] = summarize(report)
    return report


def summarize(report: dict) -> dict:
    sp, op = report["spot"], report["options"]
    return {
        "trading_days": len(sp["trading_days"]),
        "first_day": sp["trading_days"][0] if sp["trading_days"] else None,
        "last_day": sp["trading_days"][-1] if sp["trading_days"] else None,
        "weekdays_without_data": len(sp["weekdays_without_data"]),
        "incomplete_spot_days": len(sp["incomplete_days"]),
        "spot_rows_dropped": sp["dropped_bad_ohlc"] + sp["dropped_bad_time"],
        "option_contract_days_needed": op["contract_days_needed"],
        "option_contract_days_imported": op["contract_days_imported"],
        "strike_not_in_dataset": len(op["strike_not_in_dataset"]),
        "no_candles_that_day": len(op["no_candles_that_day"]),
        "sparse_contract_days": len(op["sparse"]),
        "option_rows_dropped": op["dropped_bad_ohlc"] + op["dropped_bad_time"],
        "days_without_expiry": len(report["days_without_expiry"]),
        "end_labelled_shifted": bool(
            sp["end_labelled_shifted"] or op["end_labelled_shifted"]
        ),
    }


def save_report(report: dict) -> Path:
    import json

    p = cache_dir() / "last_import_report.json"
    p.write_text(json.dumps(report, indent=2, default=str))
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    args = ap.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    engine = db.make_engine()
    db.init_db(engine)

    def show(stage, done, total, msg):
        print(f"[{stage}] {msg}" + (f" {done}/{total}" if total else ""), flush=True)

    try:
        report = run_free_backtest(
            engine,
            date.fromisoformat(args.date_from),
            date.fromisoformat(args.date_to),
            progress=show,
        )
    except FreeDataError as exc:
        print(f"FAILED: {exc}")
        return 1
    path = save_report(report)
    print("\nData quality:")
    for k, v in report["summary"].items():
        print(f"  {k}: {v}")
    print(f"\nFull report: {path}")
    print(f"Backtest run #{report['run_id']} is ready on the Backtest page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
