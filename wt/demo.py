"""Synthetic market + wallets to sanity-check the pipeline without API keys.

Wallet types planted in the data:
  skilled_*  buy a few minutes before real pumps and sell into them (copyable edge)
  decay_*    skilled early, random later (should look good in training, fail in test)
  bot_*      in-and-out within one minute on spikes (uncopyable with 20s latency)
  lucky_*    random trader with one giant winner
  rand_*     random entries and exits
This only proves the code behaves sensibly; real markets are much harder.
"""
import hashlib
import math
import random
import time

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _fake_address(name):
    """Stable, Solana-looking address for a demo name, so shortened names stay distinct."""
    n = int.from_bytes(hashlib.sha256(name.encode()).digest(), "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return out[:44]

from . import db
from .backtest import walk_forward

SOL_USD = 150.0


class Market:
    def __init__(self, rng, start, days=30, n_tokens=220):
        self.rng, self.tokens = rng, {}
        for i in range(n_tokens):
            mint = f"TOKEN{i:04d}" + "x" * 30
            t0 = int(start + rng.random() * (days - 0.5) * 86400) // 60 * 60
            n = 8 * 60
            pump_at = rng.randint(30, n - 150) if rng.random() < 0.5 else None
            pump_len = rng.randint(30, 90)
            rug_at = rng.randint(60, n - 1) if rng.random() < 0.25 else None
            spikes = set(rng.sample(range(10, n - 5), 4))
            px, candles = rng.uniform(1e-5, 1e-3), []
            for k in range(n):
                drift = -0.0012  # memecoins bleed by default
                if pump_at is not None and pump_at <= k < pump_at + pump_len:
                    drift = 0.012
                elif pump_at is not None and pump_at + pump_len <= k < pump_at + 2 * pump_len:
                    drift = -0.014
                o = px
                c = o * math.exp(drift + rng.gauss(0, 0.02))
                if rug_at is not None and k == rug_at:
                    c = o * 0.03
                h, l = max(o, c) * (1 + abs(rng.gauss(0, 0.01))), min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
                if k in spikes:
                    h, c = o * 1.14, o * 1.0
                candles.append((t0 + k * 60, o, h, l, c, 1000.0))
                px = c
            self.tokens[mint] = {"t0": t0, "candles": candles, "pump_at": pump_at, "pump_len": pump_len,
                                 "rug_at": rug_at, "spikes": sorted(spikes)}

    def price(self, mint, t):
        tk = self.tokens[mint]
        k = min(max(int((t - tk["t0"]) // 60), 0), len(tk["candles"]) - 1)
        c = tk["candles"][k]
        f = min(max((t - c[0]) / 60, 0), 1)
        return c[1] + (c[4] - c[1]) * f

    def candles(self, mint, start, end):
        return [c for c in self.tokens[mint]["candles"] if start // 60 * 60 <= c[0] <= end]


def _trade(m, wallet, mint, t, side, sol, n, px=None):
    px = px or m.price(mint, t)
    return {"sig": f"{wallet}-{n}-{side}", "wallet": wallet, "mint": mint, "ts": int(t), "side": side,
            "token_amt": sol * SOL_USD / px, "sol_amt": sol, "source": "DEMO"}


def make_wallets(m, rng, split_ts):
    trades, n = [], 0
    toks = list(m.tokens)
    pumpers = [k for k in toks if m.tokens[k]["pump_at"] is not None and m.tokens[k]["rug_at"] is None]

    def roundtrip(w, mint, t_in, t_out, sol):
        nonlocal n
        n += 1
        b = _trade(m, w, mint, t_in, "buy", sol, n)
        px_out = m.price(mint, t_out)
        out_sol = sol * px_out / m.price(mint, t_in) * 0.99
        s = {**b, "sig": f"{w}-{n}-sell", "ts": int(t_out), "side": "sell", "sol_amt": out_sol}
        trades.extend([b, s])

    def skilled_trip(w, mint):
        tk = m.tokens[mint]
        t_in = tk["t0"] + (tk["pump_at"] - rng.randint(2, 6)) * 60 + rng.randint(0, 59)
        t_out = tk["t0"] + (tk["pump_at"] + int(tk["pump_len"] * rng.uniform(0.7, 1.0))) * 60
        roundtrip(w, mint, t_in, t_out, rng.uniform(1, 4))

    def random_trip(w, mint):
        tk = m.tokens[mint]
        k = rng.randint(5, 300)
        t_in = tk["t0"] + k * 60 + rng.randint(0, 59)
        roundtrip(w, mint, t_in, t_in + rng.randint(5, 120) * 60, rng.uniform(0.5, 3))

    for i in range(3):
        w = f"skilled_{i}" + "s" * 30
        for mint in rng.sample(pumpers, min(len(pumpers), 45)):
            skilled_trip(w, mint)
        for mint in rng.sample(toks, 12):
            random_trip(w, mint)
    for i in range(2):
        w = f"decay_{i}" + "d" * 30
        for mint in toks:
            if m.tokens[mint]["t0"] < split_ts and mint in pumpers and rng.random() < 0.5:
                skilled_trip(w, mint)
            elif m.tokens[mint]["t0"] >= split_ts and rng.random() < 0.3:
                random_trip(w, mint)
    for i in range(2):
        w = f"bot_{i}" + "b" * 30
        for mint in rng.sample(toks, 60):
            tk = m.tokens[mint]
            k = rng.choice(tk["spikes"])
            c = tk["candles"][k]
            n += 1
            b = _trade(m, w, mint, c[0] + 5, "buy", 2.0, n, px=c[1])
            s = {**b, "sig": f"{w}-{n}-sell", "ts": c[0] + 50, "side": "sell", "sol_amt": 2.0 * 1.12}
            trades.extend([b, s])
    w = "lucky_0" + "l" * 30
    for mint in rng.sample(toks, 40):
        random_trip(w, mint)
    n += 1
    big = rng.choice(toks)
    b = _trade(m, w, big, m.tokens[big]["t0"] + 600, "buy", 2.0, n)
    trades.extend([b, {**b, "sig": f"{w}-{n}-sell", "ts": b["ts"] + 3600, "side": "sell", "sol_amt": 80.0}])
    for i in range(10):
        w = f"rand_{i}" + "r" * 30
        for mint in rng.sample(toks, 35):
            random_trip(w, mint)
    return trades


def _build(cfg):
    rng = random.Random(42)
    start = 1_780_000_000
    m = Market(rng, start)
    split_ts = start + 30 * 86400 * cfg["backtest"]["train_frac"]
    con = db.connect(":memory:")
    db.insert_trades(con, make_wallets(m, rng, split_ts))
    return con, m


def run_demo(cfg):
    con, m = _build(cfg)
    print("SYNTHETIC DEMO: fake market, fake wallets. Checks the logic, says nothing about real profits.\n")
    walk_forward(con, cfg, m, baseline=True)


def seed_dashboard_demo(cfg, path, now=None, n_open=2):
    """Fill a fresh database at `path` with a paper account built from the synthetic backtest's
    copy trades, shifted so the last one ends `now`. The newest `n_open` trades are left open."""
    con, m = _build(cfg)
    res = walk_forward(con, cfg, m, log=lambda *_: None)["result"]
    trades = sorted(res["closed"], key=lambda c: c["exit_ts"])
    now = int(now or time.time())
    shift = now - 300 - int(trades[-1]["exit_ts"])
    out = db.connect(path)
    for t in ("paper_account", "positions", "whale_holdings", "events", "equity_snapshots"):
        out.execute(f"DELETE FROM {t}")
    p = cfg["backtest"]
    started = int(trades[0]["entry_ts"]) + shift - 600
    cash = p["start_sol"]
    rng = random.Random(11)
    trades = [{**c, "wallet": _fake_address(c["wallet"]), "mint": _fake_address(c["mint"])} for c in trades]
    for i, c in enumerate(trades):
        e_ts, x_ts = int(c["entry_ts"]) + shift, int(c["exit_ts"]) + shift
        is_open = i >= len(trades) - n_open
        if is_open:  # opened recently, still running
            e_ts = now - max(x_ts - e_ts, 600) // 2
        cash -= c["size"]
        out.execute("INSERT INTO events VALUES(?,?,?)",
                    (e_ts, "BUY", f"{c['mint'][:6]}… {c['size']:.3f} SOL copying {c['wallet'][:6]}"))
        if is_open:
            # mark open positions at a price drawn from where the trade was heading
            value = c["size"] * (1 + rng.uniform(-0.2, 0.35))
            out.execute("INSERT INTO positions(mint,wallet,signal_sig,signal_ts,entry_ts,cost_sol,token_raw,"
                        "peak_value,last_value,last_quote_ts,status) VALUES(?,?,?,?,?,?,?,?,?,?,'open')",
                        (c["mint"], c["wallet"], "demo", e_ts - p["delay_sec"], e_ts,
                         c["size"], "0", max(value, c["size"]), value, now))
            continue
        cash += c["proceeds"]
        out.execute("INSERT INTO positions(mint,wallet,signal_sig,signal_ts,entry_ts,cost_sol,token_raw,"
                    "peak_value,last_value,last_quote_ts,status,exit_ts,proceeds_sol,exit_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'closed',?,?,?)",
                    (c["mint"], c["wallet"], "demo", e_ts - p["delay_sec"], e_ts, c["size"], "0",
                     c["proceeds"], c["proceeds"], x_ts, x_ts, c["proceeds"], c["reason"]))
        out.execute("INSERT INTO events VALUES(?,?,?)",
                    (x_ts, "SELL", f"{c['mint'][:6]}… {c['reason']}: {c['pnl']:+.3f} SOL"))
    out.execute("INSERT INTO paper_account VALUES(1,?,?,?)", (cash, p["start_sol"], started))
    out.commit()
    return out
