"""Local web dashboard for the paper account.

Serves one page plus a JSON endpoint that reads the same SQLite database `live`
writes to (read-only), so it can run alongside `live` and refreshes on its own.
"""
import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("dashboard.html")
LIVE_WINDOW_SEC = 10 * 60  # "live" if the trader logged anything this recently


def _ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql, *args):
    try:
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.OperationalError:  # table missing in an older database
        return []


def build_state(con, now=None, demo=False):
    now = int(now or time.time())
    acc = (_rows(con, "SELECT * FROM paper_account WHERE id=1") or [None])[0]
    state = {"now": now, "demo": demo, "account": None}
    if not acc:
        return state

    start = acc["start_sol"]
    opens = _rows(con, "SELECT * FROM positions WHERE status='open' ORDER BY entry_ts DESC")
    closed = _rows(con, "SELECT * FROM positions WHERE status='closed' ORDER BY exit_ts")
    open_val = sum(p["last_value"] or 0 for p in opens)
    equity = acc["cash_sol"] + open_val

    # Equity history: live snapshots (marked to market) when there are enough, else realized PnL steps.
    snaps = _rows(con, "SELECT ts, cash + open_value AS eq FROM equity_snapshots ORDER BY ts")
    if len(snaps) >= 2:
        curve, curve_kind = [[s["ts"], s["eq"]] for s in snaps], "marked"
    else:
        curve, eq = [[acc["started_at"], start]], start
        for c in closed:
            eq += c["proceeds_sol"] - c["cost_sol"]
            curve.append([c["exit_ts"], eq])
        curve_kind = "realized"
    if curve[-1][0] < now:
        curve.append([now, equity])

    peak, mdd = -1e18, 0.0
    for _, e in curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)

    pnls = [c["proceeds_sol"] - c["cost_sol"] for c in closed]
    wins = [x for x in pnls if x > 0]
    by_wallet, reasons = {}, {}
    for c, x in zip(closed, pnls):
        w = by_wallet.setdefault(c["wallet"], {"wallet": c["wallet"], "trades": 0, "wins": 0, "pnl": 0.0})
        w["trades"] += 1
        w["wins"] += x > 0
        w["pnl"] += x
        r = reasons.setdefault(c["exit_reason"], {"reason": c["exit_reason"], "count": 0, "pnl": 0.0})
        r["count"] += 1
        r["pnl"] += x

    events = _rows(con, "SELECT ts, kind, msg FROM events ORDER BY ts DESC, rowid DESC LIMIT 80")
    last_ts = events[0]["ts"] if events else 0

    state.update(
        account={"start": start, "cash": acc["cash_sol"], "open_value": open_val, "equity": equity,
                 "return": equity / start - 1 if start else 0.0, "started_at": acc["started_at"],
                 "realized": sum(pnls), "unrealized": open_val - sum(p["cost_sol"] for p in opens),
                 "closed": len(closed), "win_rate": len(wins) / len(closed) if closed else None,
                 "avg_win": sum(wins) / len(wins) if wins else None,
                 "avg_loss": (sum(x for x in pnls if x <= 0) / (len(pnls) - len(wins))) if len(pnls) > len(wins) else None,
                 "max_drawdown": mdd},
        status={"last_event_ts": last_ts or None, "live": bool(last_ts) and now - last_ts <= LIVE_WINDOW_SEC},
        curve=curve, curve_kind=curve_kind,
        open=[{"mint": p["mint"], "wallet": p["wallet"], "cost": p["cost_sol"], "value": p["last_value"],
               "peak": p["peak_value"], "entry_ts": p["entry_ts"], "quote_ts": p["last_quote_ts"],
               "no_route_since": p["no_route_since"]} for p in opens],
        trades=[{"mint": c["mint"], "wallet": c["wallet"], "cost": c["cost_sol"], "proceeds": c["proceeds_sol"],
                 "pnl": x, "entry_ts": c["entry_ts"], "exit_ts": c["exit_ts"], "reason": c["exit_reason"]}
                for c, x in list(zip(closed, pnls))[::-1][:100]],
        wallets=sorted(by_wallet.values(), key=lambda w: -w["pnl"]),
        reasons=sorted(reasons.values(), key=lambda r: -r["count"]),
        events=events,
    )
    return state


def make_handler(db_path, demo=False):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                if not Path(db_path).exists():
                    state = {"now": int(time.time()), "demo": demo, "account": None}
                else:
                    con = _ro(db_path)
                    try:
                        state = build_state(con, demo=demo)
                    finally:
                        con.close()
                self._send(200, json.dumps(state), "application/json")
            else:
                self._send(404, "not found", "text/plain")

        def log_message(self, *a):  # keep the terminal quiet
            pass

    return Handler


def serve(db_path, host="127.0.0.1", port=8050, demo=False):
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, demo))
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"Dashboard on http://{shown}:{port}  (reading {db_path}{', demo data' if demo else ''}). Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        httpd.server_close()
