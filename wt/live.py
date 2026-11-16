"""Live paper trader.

Listens to followed wallets over a Helius websocket, and when one buys, "buys"
the same token with fake SOL at a price taken from a real Jupiter quote for our
exact size (so price impact on thin tokens is real). Open positions are re-quoted
on the sell side every few seconds, which is what you'd actually get if you sold.
"""
import asyncio
import json
import random
import time

from .config import STABLES, WSOL
from .parse import parse_tx

LAMPORTS = 1_000_000_000


class PaperTrader:
    def __init__(self, con, cfg, jup, follow, log=print, now=time.time, sleep=asyncio.sleep, seed=None):
        self.con, self.p, self.jup = con, cfg["live"], jup
        self.follow = set(follow)
        self.log, self.now, self.sleep = log, now, sleep
        self.rng = random.Random(seed)
        self.recent_buys = {}   # mint -> {wallet: ts}
        self.busy = set()       # mints with an entry/exit in flight
        self.entering = 0       # entries in flight (count toward max_open)
        row = con.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if not row:
            con.execute("INSERT INTO paper_account VALUES(1,?,?,?)",
                        (self.p["start_sol"], self.p["start_sol"], int(self.now())))
            con.commit()

    # ---------- helpers ----------
    @property
    def cash(self):
        return self.con.execute("SELECT cash_sol FROM paper_account WHERE id=1").fetchone()[0]

    def _add_cash(self, d):
        self.con.execute("UPDATE paper_account SET cash_sol=cash_sol+? WHERE id=1", (d,))

    def event(self, kind, msg):
        self.con.execute("INSERT INTO events VALUES(?,?,?)", (int(self.now()), kind, msg))
        self.con.commit()
        self.log(f"[{time.strftime('%H:%M:%S', time.localtime(self.now()))}] {kind}: {msg}")

    def open_positions(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM positions WHERE status='open'")]

    def _is_open(self, pos_id):
        row = self.con.execute("SELECT status FROM positions WHERE id=?", (pos_id,)).fetchone()
        return bool(row) and row["status"] == "open"

    async def quote(self, *a):
        return await asyncio.to_thread(self.jup.quote, *a)

    # ---------- signals ----------
    async def on_trade(self, t):
        if t["wallet"] not in self.follow or t["mint"] in STABLES or t["mint"] == WSOL:
            return
        col = "bought" if t["side"] == "buy" else "sold"
        self.con.execute("INSERT OR IGNORE INTO whale_holdings(wallet,mint) VALUES(?,?)", (t["wallet"], t["mint"]))
        self.con.execute(f"UPDATE whale_holdings SET {col}={col}+? WHERE wallet=? AND mint=?",
                         (t["token_amt"], t["wallet"], t["mint"]))
        self.con.commit()
        short = t["mint"][:6]
        self.event("whale", f"{t['wallet'][:6]} {t['side']} {short}… for {t['sol_amt']:.2f} SOL")
        if t["side"] == "buy":
            await self.on_buy(t)
        else:
            await self.on_sell(t)

    async def on_buy(self, t):
        p = self.p
        if t["sol_amt"] < p["min_whale_buy_sol"]:
            return
        now = self.now()
        win = p["confluence_window_min"] * 60
        buys = self.recent_buys.setdefault(t["mint"], {})
        buys[t["wallet"]] = t["ts"] or now
        for w in [w for w, ts in buys.items() if now - ts > win]:
            del buys[w]
        if len(buys) < p["confluence_wallets"]:
            self.event("wait", f"{t['mint'][:6]}… {len(buys)}/{p['confluence_wallets']} wallets")
            return
        if t["mint"] in self.busy or any(x["mint"] == t["mint"] for x in self.open_positions()):
            return
        if len(self.open_positions()) + self.entering >= p["max_open"]:
            self.event("skip", f"{t['mint'][:6]}… max open positions")
            return
        await self.enter(t)

    async def on_sell(self, t):
        if not self.p["follow_whale_exit"]:
            return
        h = self.con.execute("SELECT bought, sold FROM whale_holdings WHERE wallet=? AND mint=?",
                             (t["wallet"], t["mint"])).fetchone()
        if not h or h["bought"] <= 0 or h["sold"] < 0.5 * h["bought"]:
            return
        for pos in self.open_positions():
            if pos["mint"] == t["mint"] and pos["wallet"] == t["wallet"]:
                await self.exit(pos, "whale_exit")

    # ---------- execution ----------
    async def enter(self, t):
        p, mint = self.p, t["mint"]
        self.busy.add(mint)
        self.entering += 1
        try:
            await self.sleep(p["exec_latency_sec"])
            size = min(p["size_sol"], self.cash - p["reserve_sol"])
            if size < 0.05:
                self.event("skip", f"{mint[:6]}… not enough paper SOL")
                return
            if self.rng.random() < p["fail_rate"]:
                self._add_cash(-p["fee_sol"])
                self.event("fail", f"{mint[:6]}… simulated failed tx (fee lost)")
                return
            # Reserve the cash before awaiting the quote so concurrent entries can't overspend.
            self._add_cash(-size)
            spend = size - p["fee_sol"]
            q = await self.quote(WSOL, mint, int(spend * LAMPORTS), p["slippage_bps"])
            if not q or q["out"] <= 0:
                self._add_cash(size)
                self.con.commit()
                self.event("skip", f"{mint[:6]}… no Jupiter route")
                return
            tokens = int(q["out"] * (1 - p["fill_haircut_bps"] / 1e4))
            now = int(self.now())
            self.con.execute(
                "INSERT INTO positions(mint,wallet,signal_sig,signal_ts,entry_ts,cost_sol,token_raw,"
                "peak_value,last_value,last_quote_ts,status) VALUES(?,?,?,?,?,?,?,?,?,?,'open')",
                (mint, t["wallet"], t["sig"], t["ts"], now, size, str(tokens), spend, spend, now))
            self.con.commit()
            lag = now - (t["ts"] or now)
            self.event("BUY", f"{mint[:6]}… {size:.3f} SOL (impact {q['impact']:.1%}, {lag}s after whale)")
        finally:
            self.entering -= 1
            self.busy.discard(mint)

    async def exit(self, pos, reason):
        p, mint = self.p, pos["mint"]
        if mint in self.busy or not self._is_open(pos["id"]):
            return
        self.busy.add(mint)
        try:
            await self.sleep(p["exec_latency_sec"])
            q = await self.quote(mint, WSOL, int(pos["token_raw"]), p["slippage_bps"])
            if not self._is_open(pos["id"]):
                return
            if not q:
                if not pos["no_route_since"]:
                    self.con.execute("UPDATE positions SET no_route_since=? WHERE id=?", (int(self.now()), pos["id"]))
                    self.con.commit()
                self.event("stuck", f"{mint[:6]}… can't sell ({reason}), will retry")
                return
            proceeds = max(q["out"] / LAMPORTS * (1 - p["fill_haircut_bps"] / 1e4) - p["fee_sol"], 0.0)
            self._close(pos, proceeds, reason)
        finally:
            self.busy.discard(mint)

    def _close(self, pos, proceeds, reason):
        cur = self.con.execute("UPDATE positions SET status='closed', exit_ts=?, proceeds_sol=?, exit_reason=? "
                               "WHERE id=? AND status='open'", (int(self.now()), proceeds, reason, pos["id"]))
        if cur.rowcount != 1:  # already closed by another path (e.g. whale exit vs. monitor)
            self.con.commit()
            return
        self._add_cash(proceeds)
        self.con.commit()
        pnl = proceeds - pos["cost_sol"]
        self.event("SELL", f"{pos['mint'][:6]}… {reason}: {pnl:+.3f} SOL ({pnl / pos['cost_sol']:+.0%}). "
                           f"Cash {self.cash:.3f} SOL")

    async def monitor_once(self):
        p = self.p
        for pos in self.open_positions():
            if pos["mint"] in self.busy:
                continue
            now = self.now()
            q = await self.quote(pos["mint"], WSOL, int(pos["token_raw"]), p["slippage_bps"])
            if pos["mint"] in self.busy or not self._is_open(pos["id"]):
                continue  # an exit started or finished while we were quoting
            if not q:
                since = pos["no_route_since"] or int(now)
                self.con.execute("UPDATE positions SET no_route_since=? WHERE id=?", (since, pos["id"]))
                self.con.commit()
                if now - since >= p["write_off_min"] * 60:
                    self._close(pos, 0.0, "rugged_no_route")
                continue
            value = q["out"] / LAMPORTS
            peak = max(pos["peak_value"], value)
            self.con.execute("UPDATE positions SET last_value=?, peak_value=?, last_quote_ts=?, no_route_since=NULL "
                             "WHERE id=?", (value, peak, int(now), pos["id"]))
            self.con.commit()
            pos.update(last_value=value, peak_value=peak, no_route_since=None)
            ret = value / pos["cost_sol"] - 1
            reason = None
            if ret >= p["take_profit"]:
                reason = "take_profit"
            elif ret <= -p["stop_loss"]:
                reason = "stop_loss"
            elif p["trailing_stop"] and value <= peak * (1 - p["trailing_stop"]) and peak > pos["cost_sol"]:
                reason = "trailing"
            elif now - pos["entry_ts"] >= p["max_hold_min"] * 60:
                reason = "max_hold"
            if reason:
                await self.exit(pos, reason)


async def run_live(con, cfg, helius, jup, follow, log=print):
    import websockets

    trader = PaperTrader(con, cfg, jup, follow, log=log)
    log(f"Paper trading {len(follow)} wallets. Cash {trader.cash:.3f} SOL. Ctrl+C to stop.")
    min_sol = cfg["collect"]["min_sol"]

    tasks = set()  # keep references so running handlers aren't garbage-collected

    async def handle(wallet, sig):
        try:
            for _ in range(6):  # parsed data can lag a moment behind confirmation
                txs = await asyncio.to_thread(helius.parse, [sig])
                if txs:
                    t = parse_tx(txs[0], wallet, min_sol)
                    if t:
                        await trader.on_trade(t)
                    return
                await asyncio.sleep(1.5)
            log(f"gave up parsing {sig[:10]}… for {wallet[:6]}")
        except Exception as e:
            log(f"error handling {sig[:10]}… for {wallet[:6]}: {e!r}")

    async def monitor():
        while True:
            try:
                await trader.monitor_once()
            except Exception as e:  # keep running through flaky API calls
                log(f"monitor error: {e}")
            await asyncio.sleep(cfg["live"]["monitor_sec"])

    async def listen():
        backoff = 1
        while True:
            try:
                async with websockets.connect(helius.ws_url(), ping_interval=20, max_size=2**22) as ws:
                    pending = {}
                    for i, w in enumerate(sorted(follow), 1):
                        pending[i] = w
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                                  "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                    subs = {}
                    backoff = 1
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "id" in msg and msg.get("id") in pending:
                            if "result" in msg:
                                subs[msg["result"]] = pending[msg["id"]]
                            else:
                                log(f"subscribe failed for {pending[msg['id']][:6]}: {msg.get('error')}")
                            if len(subs) == len(follow):
                                log(f"Listening to {len(subs)} wallets.")
                            continue
                        par = msg.get("params") or {}
                        val = ((par.get("result") or {}).get("value")) or {}
                        w = subs.get(par.get("subscription"))
                        if w and val.get("signature") and not val.get("err"):
                            task = asyncio.create_task(handle(w, val["signature"]))
                            tasks.add(task)
                            task.add_done_callback(tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"websocket dropped ({e}); reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    await asyncio.gather(listen(), monitor())
