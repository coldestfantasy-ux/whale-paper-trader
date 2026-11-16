import asyncio

from wt import db
from wt.backtest import run_portfolio, simulate_trade
from wt.config import DEFAULTS, WSOL
from wt.live import PaperTrader
from wt.parse import parse_tx
from wt.score import build_trips, score_wallet

W = "Wha1e" + "1" * 39
MINT = "Meme" + "2" * 40
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def tok(user, mint, raw, dec=6):
    return {"userAccount": user, "tokenAccount": "ata", "mint": mint,
            "rawTokenAmount": {"tokenAmount": str(raw), "decimals": dec}}


def tx(native, token_changes, sig="s1", ts=1000, err=None):
    return {"signature": sig, "timestamp": ts, "source": "PUMP_FUN", "transactionError": err,
            "accountData": [{"account": W, "nativeBalanceChange": native, "tokenBalanceChanges": []},
                            {"account": "ata", "nativeBalanceChange": 0, "tokenBalanceChanges": token_changes}]}


# ---------- parsing ----------
def test_parse_buy_native_sol():
    t = parse_tx(tx(-1_500_000_000, [tok(W, MINT, 5_000_000_000)]), W)
    assert t["side"] == "buy" and abs(t["sol_amt"] - 1.5) < 1e-9 and t["token_amt"] == 5000


def test_parse_buy_with_wsol_wrap_not_double_counted():
    # wallet wraps 2 SOL into its wSOL account and spends it: native -2, wSOL net 0
    t = parse_tx(tx(-2_000_000_000, [tok(W, WSOL, 0, 9), tok(W, MINT, 10_000)]), W)
    assert t["side"] == "buy" and abs(t["sol_amt"] - 2.0) < 1e-9


def test_parse_sell_paid_in_wsol():
    t = parse_tx(tx(-5000, [tok(W, MINT, -10_000_000), tok(W, WSOL, 3_000_000_000, 9)]), W)
    assert t["side"] == "sell" and abs(t["sol_amt"] - (3 - 0.000005)) < 1e-9


def test_parse_skips_stable_transfers_and_errors():
    assert parse_tx(tx(0, [tok(W, USDC, -100_000_000), tok(W, MINT, 1000)]), W) is None
    assert parse_tx(tx(-5000, [tok(W, MINT, 1000)]), W) is None          # airdrop/transfer
    assert parse_tx(tx(-1_500_000_000, [tok(W, MINT, 1000)], err="x"), W) is None
    assert parse_tx(tx(-1_500_000_000, [tok("other", MINT, 1000)]), W) is None


# ---------- trips & scoring ----------
def tr(side, ts, tokens, sol, mint=MINT):
    return {"sig": f"{side}{ts}", "wallet": W, "mint": mint, "ts": ts, "side": side,
            "token_amt": tokens, "sol_amt": sol, "source": ""}


def test_build_trips_partial_sells_and_half_exit():
    trips = build_trips([tr("buy", 0, 100, 1.0), tr("buy", 60, 100, 1.0),
                         tr("sell", 600, 120, 3.0), tr("sell", 1200, 80, 1.0)])
    t = trips[0]
    assert t["closed"] and t["half_exit_ts"] == 600 and abs(t["pnl"] - 2.0) < 1e-9
    assert t["hold_min"] == 20


def test_score_flags_one_hit_wonder():
    trades = []
    for i in range(12):
        trades += [tr("buy", i * 1000, 1, 1.0, f"m{i}"), tr("sell", i * 1000 + 600, 1, 0.8, f"m{i}")]
    trades += [tr("buy", 99000, 1, 1.0, "big"), tr("sell", 99600, 1, 50.0, "big")]
    m = score_wallet(build_trips(trades), DEFAULTS["scoring"])
    assert not m["passes"] and any("one trade" in r for r in m["reasons"])


# ---------- trade simulation ----------
P = {**DEFAULTS["backtest"], "slippage_bps": 0, "delay_sec": 0, "fee_sol": 0, "fail_rate": 0}


def c(ts, o, h, l, cl):
    return (ts, o, h, l, cl, 1.0)


def test_stop_checked_before_take_profit_in_same_candle():
    candles = [c(0, 1, 1, 1, 1), c(60, 1, 2.0, 0.5, 1)]
    o = simulate_trade(0, None, candles, P)
    assert o["reason"] == "stop_loss" and abs(o["mult"] - 0.75) < 1e-9


def test_take_profit_and_gap_through_stop():
    o = simulate_trade(0, None, [c(0, 1, 1, 1, 1), c(60, 1.1, 1.6, 1.05, 1.5)], P)
    assert o["reason"] == "take_profit" and abs(o["mult"] - 1.5) < 1e-9
    o = simulate_trade(0, None, [c(0, 1, 1, 1, 1), c(60, 0.3, 0.3, 0.2, 0.25)], P)
    assert abs(o["mult"] - 0.3) < 1e-9  # gapped below the stop: filled at the open, not the stop


def test_whale_exit_and_no_data():
    candles = [c(0, 1, 1, 1, 1), c(60, 1.1, 1.2, 1.05, 1.2), c(120, 1.2, 1.3, 1.15, 1.3)]
    o = simulate_trade(0, 120, candles, P)
    assert o["reason"] == "whale_exit" and abs(o["mult"] - 1.2) < 1e-9
    assert simulate_trade(10_000, None, candles, P) is None


def test_portfolio_costs():
    p = {**P, "fee_sol": 0.01, "size_sol": 1.0, "start_sol": 10.0}
    r = run_portfolio([{"entry_ts": 0, "exit_ts": 60, "mult": 1.0, "mint": "a", "wallet": W, "reason": "x"}], p)
    assert abs(r["pnl_sol"] - (-0.02)) < 1e-9  # flat trade still loses two fees


# ---------- live paper trader ----------
class FakeJup:
    def __init__(self):
        self.sell_value_sol = None  # None = no route

    def quote(self, in_mint, out_mint, amount, slip):
        if in_mint == WSOL:
            return {"out": amount * 1000, "impact": 0.01}
        if self.sell_value_sol is None:
            return None
        return {"out": int(self.sell_value_sol * 1e9), "impact": 0.01}


class Clock:
    t = 10_000.0

    def __call__(self):
        return self.t


async def nosleep(_):
    return None


def make_trader(**live):
    cfg = {"live": {**DEFAULTS["live"], "fail_rate": 0, **live}}
    con = db.connect(":memory:")
    jup, clock = FakeJup(), Clock()
    t = PaperTrader(con, cfg, jup, [W, "W2"], log=lambda *_: None, now=clock, sleep=nosleep, seed=1)
    return t, jup, clock, con


def buy(wallet=W, ts=10_000, sol=2.0):
    return {"sig": f"b{wallet}{ts}", "wallet": wallet, "mint": MINT, "ts": ts, "side": "buy",
            "token_amt": 1000.0, "sol_amt": sol, "source": ""}


def test_live_entry_and_take_profit():
    t, jup, clock, con = make_trader()
    asyncio.run(t.on_trade(buy()))
    pos = t.open_positions()
    assert len(pos) == 1 and abs(t.cash - 9.5) < 1e-9
    jup.sell_value_sol = 0.9  # +80%
    asyncio.run(t.monitor_once())
    row = con.execute("SELECT * FROM positions").fetchone()
    assert row["status"] == "closed" and row["exit_reason"] == "take_profit"
    assert t.cash > 10.3


def test_live_small_whale_buy_ignored_and_confluence():
    t, *_ = make_trader(confluence_wallets=2)
    asyncio.run(t.on_trade(buy(sol=0.1)))
    asyncio.run(t.on_trade(buy()))
    assert not t.open_positions()
    asyncio.run(t.on_trade(buy(wallet="W2")))
    assert len(t.open_positions()) == 1


def test_live_rug_written_off_after_no_route():
    t, jup, clock, con = make_trader()
    asyncio.run(t.on_trade(buy()))
    asyncio.run(t.monitor_once())
    assert t.open_positions()
    clock.t += 16 * 60
    asyncio.run(t.monitor_once())
    row = con.execute("SELECT * FROM positions").fetchone()
    assert row["exit_reason"] == "rugged_no_route" and row["proceeds_sol"] == 0


def test_live_follows_whale_exit():
    t, jup, clock, con = make_trader()
    asyncio.run(t.on_trade(buy()))
    jup.sell_value_sol = 0.5
    sell = {**buy(), "sig": "s", "side": "sell", "token_amt": 600.0, "sol_amt": 2.5}
    asyncio.run(t.on_trade(sell))
    assert con.execute("SELECT exit_reason FROM positions").fetchone()[0] == "whale_exit"


class SlowJup(FakeJup):
    """Quotes take `delays[n]` seconds of event-loop time for the n-th call, so tasks interleave."""

    def __init__(self, delays):
        super().__init__()
        self.delays, self.n = delays, 0


def make_async_trader(jup, **live):
    cfg = {"live": {**DEFAULTS["live"], "fail_rate": 0, "exec_latency_sec": 0.0, **live}}
    con = db.connect(":memory:")
    t = PaperTrader(con, cfg, jup, [W, "W2"], log=lambda *_: None, now=Clock(), seed=1)

    async def quote(*a):
        d = jup.delays[jup.n] if jup.n < len(jup.delays) else 0.0
        jup.n += 1
        await asyncio.sleep(d)
        return jup.quote(*a)
    t.quote = quote
    return t, con


def test_live_whale_exit_during_monitor_quote_closes_once():
    # call 0 = entry quote, call 1 = monitor's slow quote, call 2 = whale-exit quote (fast)
    jup = SlowJup([0.0, 0.05, 0.0])
    t, con = make_async_trader(jup)

    async def go():
        await t.on_trade(buy())
        jup.sell_value_sol = 0.2  # deep below the stop, so the monitor wants out too
        sell = {**buy(), "sig": "s", "side": "sell", "token_amt": 1000.0, "sol_amt": 2.5}
        await asyncio.gather(t.monitor_once(), t.on_trade(sell))
    asyncio.run(go())
    rows = con.execute("SELECT status, exit_reason, proceeds_sol FROM positions").fetchall()
    assert len(rows) == 1 and rows[0]["exit_reason"] == "whale_exit"
    assert abs(t.cash - (9.5 + rows[0]["proceeds_sol"])) < 1e-9  # credited exactly once


def test_live_concurrent_entries_respect_max_open_and_cash():
    jup = SlowJup([0.02] * 10)
    t, con = make_async_trader(jup, max_open=2)

    async def go():
        await asyncio.gather(*(t.on_trade({**buy(), "mint": f"M{i}", "sig": f"s{i}"}) for i in range(4)))
    asyncio.run(go())
    assert len(t.open_positions()) == 2 and abs(t.cash - 9.0) < 1e-9

    jup2 = SlowJup([0.02] * 10)
    t2, _ = make_async_trader(jup2, start_sol=1.0, reserve_sol=0.1, max_open=5)

    async def go2():
        await asyncio.gather(*(t2.on_trade({**buy(), "mint": f"M{i}", "sig": f"s{i}"}) for i in range(3)))
    asyncio.run(go2())
    assert t2.cash >= 0.1 - 1e-9  # never spends into the reserve


def test_live_no_route_entry_refunds_cash():
    class NoBuyRoute(FakeJup):
        def quote(self, in_mint, *a):
            return None
    t, *_ = make_trader()
    t.jup = NoBuyRoute()
    asyncio.run(t.on_trade(buy()))
    assert not t.open_positions() and abs(t.cash - 10.0) < 1e-9
