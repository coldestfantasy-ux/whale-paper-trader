import asyncio
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from wt import config, dashboard, db
from wt.config import DEFAULTS
from wt.demo import seed_dashboard_demo
from wt.live import PaperTrader


def test_empty_database_has_no_account():
    s = dashboard.build_state(db.connect(":memory:"), now=1000)
    assert s["account"] is None


def _account(con, cash=9.0, start=10.0, started=1000):
    con.execute("INSERT INTO paper_account VALUES(1,?,?,?)", (cash, start, started))


def _pos(con, wallet, cost, proceeds, exit_ts, reason="take_profit", status="closed", value=None):
    con.execute("INSERT INTO positions(mint,wallet,entry_ts,cost_sol,token_raw,last_value,status,exit_ts,"
                "proceeds_sol,exit_reason) VALUES('M',?,?,?,'1',?,?,?,?,?)",
                (wallet, (exit_ts or 4900) - 60, cost, value, status, exit_ts, proceeds, reason))


def test_state_totals_curve_and_breakdowns():
    con = db.connect(":memory:")
    _account(con, cash=10.1)
    _pos(con, "A", 0.5, 1.0, 2000)                     # +0.5
    _pos(con, "B", 0.5, 0.3, 3000, "stop_loss")        # -0.2
    _pos(con, "A", 0.5, 0.4, 4000, "stop_loss")        # -0.1
    _pos(con, "B", 0.5, None, None, status="open", value=0.6)
    con.execute("INSERT INTO events VALUES(4990,'BUY','x')")
    s = dashboard.build_state(con, now=5000)
    a = s["account"]
    assert abs(a["realized"] - 0.2) < 1e-9 and abs(a["equity"] - 10.7) < 1e-9
    assert abs(a["unrealized"] - 0.1) < 1e-9 and abs(a["win_rate"] - 1 / 3) < 1e-9
    assert s["curve_kind"] == "realized"
    assert [round(v, 3) for _, v in s["curve"]] == [10.0, 10.5, 10.3, 10.2, 10.7]
    assert abs(a["max_drawdown"] - 0.3 / 10.5) < 1e-9
    assert [(w["wallet"], round(w["pnl"], 3), w["trades"]) for w in s["wallets"]] == [("A", 0.4, 2), ("B", -0.2, 1)]
    assert [(r["reason"], r["count"]) for r in s["reasons"]] == [("stop_loss", 2), ("take_profit", 1)]
    assert s["trades"][0]["exit_ts"] == 4000 and len(s["open"]) == 1
    assert s["status"]["live"] is True


def test_live_snapshots_drive_the_curve():
    class Jup:
        def quote(self, *a):
            return None

    t_now = [10_000]
    cfg = {"live": {**DEFAULTS["live"]}}
    con = db.connect(":memory:")
    trader = PaperTrader(con, cfg, Jup(), ["W"], log=lambda *_: None, now=lambda: t_now[0])
    asyncio.run(trader.monitor_once())
    asyncio.run(trader.monitor_once())  # same minute: no second snapshot
    t_now[0] += 61
    asyncio.run(trader.monitor_once())
    assert con.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 2
    s = dashboard.build_state(con, now=t_now[0])
    assert s["curve_kind"] == "marked" and s["curve"][-1][1] == 10.0


def test_demo_seed_is_internally_consistent(tmp_path):
    cfg = config.load(str(tmp_path / "none.toml"))
    con = seed_dashboard_demo(cfg, str(tmp_path / "demo.db"), now=2_000_000_000)
    s = dashboard.build_state(con, now=2_000_000_000, demo=True)
    a = s["account"]
    open_cost = sum(p["cost"] for p in s["open"])
    assert abs(a["cash"] + open_cost - (a["start"] + a["realized"])) < 1e-9  # cash accounting adds up
    assert len(s["open"]) == 2 and a["closed"] > 20
    assert max(t["exit_ts"] for t in s["trades"]) <= 2_000_000_000
    assert len({w["wallet"][:6] for w in s["wallets"]}) == len(s["wallets"])  # distinct when shortened


def test_http_server_serves_page_and_state(tmp_path):
    path = tmp_path / "t.db"
    con = db.connect(str(path))
    _account(con)
    con.commit()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(str(path)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>Whale Paper Trader</title>" in page
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["account"]["cash"] == 9.0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_server_without_database(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(str(tmp_path / "missing.db")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/state").read())
        assert state["account"] is None
        assert not (tmp_path / "missing.db").exists()  # read-only: never creates the file
    finally:
        httpd.shutdown()
        httpd.server_close()
