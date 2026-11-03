from datetime import datetime

import pytest
from sqlalchemy import select

from app import create_app, db
from app.groww_client import GrowwSession
from app.groww_data import ReadOnlyGroww

from .fakes import FakeGroww


@pytest.fixture
def app(tmp_path):
    fake = FakeGroww()
    api = ReadOnlyGroww(fake, min_interval=0, sleep=lambda s: None)
    app = create_app(
        GrowwSession(api_key=None, totp_secret=None),
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        api_factory=lambda: api,
        paper_clock=lambda: datetime(2025, 10, 11, 11, 0),
    )
    app.config["TESTING"] = True
    from app.dashboard import summary_cache, trade_cache

    summary_cache.clear()
    trade_cache.clear()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def run(app, client, **extra):
    body = {"from": "2025-09-29", "to": "2025-10-10", **extra}
    r = client.post("/api/backtest", json=body)
    assert r.status_code == 200, r.get_json()
    app.extensions["jobs"].join(120)
    st = client.get("/api/job").get_json()
    assert st["status"] == "done", st
    return st["result"]["run_id"]


def test_pages_render(client):
    for url in ("/backtest", "/data", "/paper", "/"):
        r = client.get(url)
        assert r.status_code == 200, url
    html = client.get("/paper").get_data(as_text=True)
    assert "Live trading: off" in html


def test_empty_state_before_any_run(client):
    assert client.get("/api/summary").get_json() == {"empty": True, "reason": "no_runs"}
    assert client.get("/api/runs").get_json() == []


def test_backtest_job_and_summary(app, client):
    rid = run(app, client)
    runs = client.get("/api/runs").get_json()
    assert runs[0]["id"] == rid and runs[0]["status"] == "done"
    q = f"/api/summary?run={rid}&tf=5&mode=ATM_BUY&start=09:30&cutoff=14:30"
    d = client.get(q).get_json()
    assert d["metrics"]["trades"] == len(d["trades"]) > 0
    assert set(d["modes"]) == {"ITM5_BUY", "ATM_BUY", "ATM_SELL"}
    assert d["modes"]["ATM_BUY"]["net"] == d["metrics"]["net"]
    assert round(sum(t["net"] for t in d["trades"]), 2) == d["metrics"]["net"]
    assert len(d["days"]) == 9
    # outcome + cross filters
    w = client.get(q + "&outcome=win").get_json()
    assert all(t["net"] > 0 for t in w["trades"])
    lo = client.get(q + "&outcome=loss").get_json()
    assert len(w["trades"]) + len(lo["trades"]) <= len(d["trades"])
    c1 = client.get(q + "&cross=1").get_json()
    assert all(t["cross_no"] == 1 for t in c1["trades"])
    c4 = client.get(q + "&cross=4%2B").get_json()
    assert all(t["cross_no"] >= 4 for t in c4["trades"])
    # cached second call returns same payload
    assert client.get(q).get_json() == d


def test_trade_detail_has_chart_and_costs(app, client):
    rid = run(app, client)
    d = client.get(f"/api/summary?run={rid}&tf=15&mode=ATM_SELL").get_json()
    tid = d["trades"][0]["id"]
    r = client.get(f"/api/trade/{tid}")
    assert r.status_code == 200 and "max-age" in r.headers["Cache-Control"]
    body = r.get_json()
    t, ch = body["trade"], body["chart"]
    assert t["side"] == "SELL"
    parts = (
        t["slippage"]
        + t["brokerage"]
        + t["stt"]
        + t["exchange"]
        + t["sebi"]
        + t["stamp"]
        + t["gst"]
    )
    assert abs(parts - t["friction"]) < 0.02
    assert abs(t["gross"] - t["friction"] - t["net"]) < 0.02
    spot_times = {b["time"] for b in ch["spot"]}
    opt_times = {b["time"] for b in ch["option"]}
    assert ch["entry_bar"] in spot_times and ch["exit_bar"] in spot_times
    assert ch["entry_minute"] in opt_times
    assert len(ch["ema9"]) == len(ch["spot"])  # warm-up comes from earlier days
    assert client.get("/api/trade/999999").status_code == 404


def test_bad_inputs(app, client):
    rid = run(app, client)
    assert client.get(f"/api/summary?run={rid}&tf=7").status_code == 400
    assert client.get(f"/api/summary?run={rid}&mode=X").status_code == 400
    assert client.get(f"/api/summary?run={rid}&start=09:31").status_code == 400
    assert (
        client.post(
            "/api/backtest", json={"from": "2025-10-10", "to": "2025-10-01"}
        ).status_code
        == 400
    )
    assert client.post("/api/backtest", json={"from": "bad"}).status_code == 400
    assert (
        client.post(
            "/api/backtest", json={"from": "2025-10-01", "to": "2025-10-02", "lots": 0}
        ).status_code
        == 400
    )


def test_rerun_on_stored_data_and_export(app, client):
    rid = run(app, client)
    rid2 = run(app, client, download=False, slippage_points=0, slippage_pct=0)
    assert rid2 != rid
    eng = app.extensions["db"]
    with eng.connect() as conn:
        a = conn.execute(
            select(db.trades.c.slippage).where(db.trades.c.run_id == rid2)
        ).all()
    # only rounding to the 0.05 tick is left: at most 0.05 x 65 per side
    assert a and all(abs(x[0]) <= 0.05 * 65 * 2 + 0.01 for x in a)
    csv = client.get(f"/export/run/{rid}.csv")
    assert csv.status_code == 200 and csv.data.startswith(b"id,run_id,")


def test_paper_endpoints(client):
    s = client.get("/api/paper").get_json()
    assert s["status"] == "stopped" and s["live_trading_enabled"] is False
    assert client.post("/api/paper/start", json={"timeframe": 7}).status_code == 400
    r = client.post(
        "/api/paper/start", json={"timeframe": 15, "start": "10:00", "cutoff": "13:00"}
    )
    assert r.status_code == 200
    s = client.post("/api/paper/stop").get_json()
    assert s["running"] is False


def test_db_down_shows_error_state():
    app = create_app(
        GrowwSession(api_key=None, totp_secret=None),
        database_url="postgresql+psycopg://x:y@127.0.0.1:1/nope",
    )
    c = app.test_client()
    assert "Database unavailable" in c.get("/backtest").get_data(as_text=True)
    assert c.get("/api/summary").status_code == 503
