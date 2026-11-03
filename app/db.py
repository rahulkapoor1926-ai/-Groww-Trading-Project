"""Postgres (or SQLite in tests) storage via SQLAlchemy Core."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine

from .strategy.bars import Candle

DEFAULT_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/trading"

metadata = MetaData()

candles = Table(
    "candles",
    metadata,
    Column("symbol", String(64), primary_key=True),
    Column("ts", DateTime, primary_key=True),  # IST, candle start
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Float),
    Column("oi", Float),
)

# One row per (symbol, day) we already asked Groww for, even if it was empty
# (holiday, illiquid strike), so we never download the same thing twice.
coverage = Table(
    "coverage",
    metadata,
    Column("symbol", String(64), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("rows", Integer, nullable=False),
    Column("fetched_at", DateTime, nullable=False),
)

expiries = Table(
    "expiries",
    metadata,
    Column("underlying", String(16), primary_key=True),
    Column("expiry", Date, primary_key=True),
)

expiry_months = Table(  # which (year, month) expiry lists we fetched
    "expiry_months",
    metadata,
    Column("underlying", String(16), primary_key=True),
    Column("year", Integer, primary_key=True),
    Column("month", Integer, primary_key=True),
)

contracts = Table(
    "contracts",
    metadata,
    Column("symbol", String(64), primary_key=True),
    Column("underlying", String(16), nullable=False),
    Column("expiry", Date, nullable=False, index=True),
    Column("strike", Float, nullable=False),
    Column("opt_type", String(2), nullable=False),
)

contract_lists = Table(  # expiries whose contract list we fetched
    "contract_lists",
    metadata,
    Column("underlying", String(16), primary_key=True),
    Column("expiry", Date, primary_key=True),
    Column("count", Integer, nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(16), nullable=False),  # backtest | paper
    Column("created_at", DateTime, nullable=False),
    Column("date_from", Date, nullable=False),
    Column("date_to", Date, nullable=False),
    Column("params", JSON, nullable=False),
    Column("status", String(16), nullable=False),  # running | done | failed
    Column("message", Text),
)

trades = Table(
    "trades",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, nullable=False, index=True),
    Column("timeframe", Integer, nullable=False),
    Column("start", String(5), nullable=False),
    Column("cutoff", String(5), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("day", Date, nullable=False),
    Column("direction", Integer, nullable=False),
    Column("side", String(4), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("opt_type", String(2), nullable=False),
    Column("strike", Integer, nullable=False),
    Column("expiry", Date, nullable=False),
    Column("dte", Integer, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("cross_no", Integer, nullable=False),
    Column("entry_signal_time", DateTime, nullable=False),
    Column("entry_time", DateTime, nullable=False),
    Column("entry_raw", Float, nullable=False),
    Column("entry_fill", Float, nullable=False),
    Column("entry_how", String(8), nullable=False),
    Column("spot_entry", Float),
    Column("reversal_entry", Boolean, nullable=False),
    Column("exit_signal_time", DateTime),
    Column("exit_time", DateTime, nullable=False),
    Column("exit_raw", Float, nullable=False),
    Column("exit_fill", Float, nullable=False),
    Column("exit_how", String(8), nullable=False),
    Column("exit_reason", String(16), nullable=False),
    Column("spot_exit", Float),
    Column("whipsaw", Boolean, nullable=False),
    Column("gross", Float, nullable=False),
    Column("slippage", Float, nullable=False),
    Column("brokerage", Float, nullable=False),
    Column("stt", Float, nullable=False),
    Column("exchange", Float, nullable=False),
    Column("sebi", Float, nullable=False),
    Column("stamp", Float, nullable=False),
    Column("gst", Float, nullable=False),
    Column("charges", Float, nullable=False),
    Column("friction", Float, nullable=False),
    Column("net", Float, nullable=False),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, nullable=False, index=True),
    Column("timeframe", Integer, nullable=False),
    Column("start", String(5), nullable=False),
    Column("cutoff", String(5), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("day", Date, nullable=False),
    Column("time", DateTime, nullable=False),
    Column("kind", String(24), nullable=False),
    Column("detail", Text, nullable=False),
)

days = Table(  # per run: what happened on each trading day
    "days",
    metadata,
    Column("run_id", Integer, primary_key=True),
    Column("day", Date, primary_key=True),
    Column("minutes", Integer, nullable=False),  # spot 1m candles that day
    Column("status", String(16), nullable=False),  # ok | incomplete
)


def make_engine(url: str | None = None) -> Engine:
    url = url or os.getenv("DATABASE_URL") or DEFAULT_URL
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, future=True, **kwargs)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)


def _insert_ignore(engine: Engine, table: Table):
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table)


def upsert_candles(engine: Engine, symbol: str, rows: Iterable[tuple]) -> int:
    """rows: (ts, open, high, low, close, volume, oi). Newer data wins."""
    data = [
        dict(
            symbol=symbol,
            ts=r[0],
            open=r[1],
            high=r[2],
            low=r[3],
            close=r[4],
            volume=r[5],
            oi=r[6] if len(r) > 6 else None,
        )
        for r in rows
    ]
    if not data:
        return 0
    stmt = _insert_ignore(engine, candles)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "ts"],
        set_={
            k: getattr(stmt.excluded, k)
            for k in ("open", "high", "low", "close", "volume", "oi")
        },
    )
    with engine.begin() as conn:
        for i in range(0, len(data), 5000):
            conn.execute(stmt, data[i : i + 5000])
    return len(data)


def mark_covered(engine: Engine, symbol: str, day_counts: dict[date, int]) -> None:
    if not day_counts:
        return
    now = datetime.now()
    stmt = _insert_ignore(engine, coverage)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "day"],
        set_={"rows": stmt.excluded.rows, "fetched_at": stmt.excluded.fetched_at},
    )
    with engine.begin() as conn:
        conn.execute(
            stmt,
            [
                dict(symbol=symbol, day=d, rows=n, fetched_at=now)
                for d, n in day_counts.items()
            ],
        )


def covered_days(engine: Engine, symbol: str, start: date, end: date) -> set[date]:
    q = select(coverage.c.day).where(
        coverage.c.symbol == symbol, coverage.c.day >= start, coverage.c.day <= end
    )
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(q)}


def covered_pairs(engine: Engine, symbols: Sequence[str]) -> set[tuple[str, date]]:
    out: set[tuple[str, date]] = set()
    with engine.connect() as conn:
        for i in range(0, len(symbols), 500):
            chunk = list(symbols[i : i + 500])
            q = select(coverage.c.symbol, coverage.c.day).where(
                coverage.c.symbol.in_(chunk)
            )
            out.update((r[0], r[1]) for r in conn.execute(q))
    return out


def load_candles(
    engine: Engine, symbol: str, start: datetime, end: datetime
) -> list[Candle]:
    q = (
        select(
            candles.c.ts,
            candles.c.open,
            candles.c.high,
            candles.c.low,
            candles.c.close,
            candles.c.volume,
        )
        .where(candles.c.symbol == symbol, candles.c.ts >= start, candles.c.ts < end)
        .order_by(candles.c.ts)
    )
    with engine.connect() as conn:
        return [
            Candle(r[0], r[1], r[2], r[3], r[4], r[5] or 0.0) for r in conn.execute(q)
        ]


def load_candles_multi(
    engine: Engine, symbols: Sequence[str], start: datetime, end: datetime
) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = {s: [] for s in symbols}
    with engine.connect() as conn:
        for i in range(0, len(symbols), 500):
            chunk = list(symbols[i : i + 500])
            q = (
                select(
                    candles.c.symbol,
                    candles.c.ts,
                    candles.c.open,
                    candles.c.high,
                    candles.c.low,
                    candles.c.close,
                    candles.c.volume,
                )
                .where(
                    candles.c.symbol.in_(chunk),
                    candles.c.ts >= start,
                    candles.c.ts < end,
                )
                .order_by(candles.c.symbol, candles.c.ts)
            )
            for r in conn.execute(q):
                out[r[0]].append(Candle(r[1], r[2], r[3], r[4], r[5], r[6] or 0.0))
    return out


def save_expiries(
    engine: Engine, underlying: str, year: int, month: int, dates: Iterable[date]
) -> None:
    with engine.begin() as conn:
        rows = [dict(underlying=underlying, expiry=d) for d in dates]
        if rows:
            conn.execute(
                _insert_ignore(engine, expiries).on_conflict_do_nothing(), rows
            )
        conn.execute(
            _insert_ignore(engine, expiry_months).on_conflict_do_nothing(),
            [dict(underlying=underlying, year=year, month=month)],
        )


def fetched_expiry_months(engine: Engine, underlying: str) -> set[tuple[int, int]]:
    q = select(expiry_months.c.year, expiry_months.c.month).where(
        expiry_months.c.underlying == underlying
    )
    with engine.connect() as conn:
        return {(r[0], r[1]) for r in conn.execute(q)}


def load_expiries(engine: Engine, underlying: str) -> list[date]:
    q = (
        select(expiries.c.expiry)
        .where(expiries.c.underlying == underlying)
        .order_by(expiries.c.expiry)
    )
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(q)]


def save_contracts(
    engine: Engine, underlying: str, expiry: date, rows: list[dict]
) -> None:
    with engine.begin() as conn:
        if rows:
            conn.execute(
                _insert_ignore(engine, contracts).on_conflict_do_nothing(), rows
            )
        stmt = _insert_ignore(engine, contract_lists)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["underlying", "expiry"],
                set_={"count": stmt.excluded.count},
            ),
            [dict(underlying=underlying, expiry=expiry, count=len(rows))],
        )


def fetched_contract_lists(engine: Engine, underlying: str) -> set[date]:
    q = select(contract_lists.c.expiry).where(contract_lists.c.underlying == underlying)
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(q)}


def load_contracts(engine: Engine, expiry_list: Iterable[date]) -> dict[tuple, str]:
    """{(expiry, strike, opt_type): symbol}"""
    exps = list(expiry_list)
    out: dict[tuple, str] = {}
    if not exps:
        return out
    with engine.connect() as conn:
        q = select(
            contracts.c.symbol,
            contracts.c.expiry,
            contracts.c.strike,
            contracts.c.opt_type,
        ).where(contracts.c.expiry.in_(exps))
        for r in conn.execute(q):
            out[(r[1], int(r[2]), r[3])] = r[0]
    return out


def data_summary(engine: Engine, spot_symbol: str) -> dict:
    with engine.connect() as conn:
        first, last, n = conn.execute(
            select(func.min(candles.c.ts), func.max(candles.c.ts), func.count()).where(
                candles.c.symbol == spot_symbol
            )
        ).one()
        opt_syms = conn.execute(
            select(func.count(func.distinct(candles.c.symbol))).where(
                candles.c.symbol != spot_symbol
            )
        ).scalar()
        opt_rows = conn.execute(
            select(func.count())
            .select_from(candles)
            .where(candles.c.symbol != spot_symbol)
        ).scalar()
    return {
        "spot_first": first,
        "spot_last": last,
        "spot_rows": n,
        "option_symbols": opt_syms,
        "option_rows": opt_rows,
    }


def delete_run_rows(conn, run_id: int) -> None:
    for t in (trades, events, days):
        conn.execute(delete(t).where(t.c.run_id == run_id))
