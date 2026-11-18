"""Wallet hunt: run detection, early-buyer + hold filters, and the out-of-sample guard."""
import pytest

from wt import db
from wt.backtest import walk_forward
from wt.config import DEFAULTS
from wt.hunt import find_run, hunt

H = 3600
RUN = dict(min_mult=5, max_mult=10, min_run_hours=24, max_run_days=14, early_mult=2, early_max_hours=24)


def hourly(prices, t0=0):
    return [(t0 + i * H, p, p, p, p, 1.0) for i, p in enumerate(prices)]


def test_find_run_accepts_5_to_10x_over_days():
    prices = [1.0] * 10 + [1.0 + 0.1 * i for i in range(1, 71)]  # 1 -> 8 over 70 hours
    r = find_run(hourly(prices), **RUN)
    assert r and 7.5 < r["mult"] <= 8.0
    assert r["low_ts"] == 9 * H  # last hour at the bottom
    # early window ends the hour after price first closes at 2x the low (hour 19 -> ends 20)
    assert r["early_end_ts"] == 20 * H


@pytest.mark.parametrize("prices", [
    [1.0] * 10 + [3.0] * 50,                     # only 3x
    [1.0] * 10 + [1 + 0.5 * i for i in range(1, 80)],  # 40x: more than 10x
    [1.0] * 10 + [8.0] * 50,                     # 8x but within one hour, not over days
])
def test_find_run_rejects(prices):
    if prices[-1] == 8.0:
        assert find_run(hourly(prices), **{**RUN, "min_run_hours": 24}) is not None  # low->high gap is fine...
        assert find_run(hourly(prices), **{**RUN, "max_run_days": 0.5}) is None      # ...unless the window is short
    else:
        assert find_run(hourly(prices), **RUN) is None


def swap(sig, wallet, mint, ts, side, sol=1.0, amt=1000.0):
    s = -1 if side == "buy" else 1
    return {"signature": sig, "timestamp": ts, "feePayer": wallet, "source": "TEST",
            "accountData": [{"account": wallet, "nativeBalanceChange": int(s * sol * 1e9),
                             "tokenBalanceChanges": [{"userAccount": wallet, "mint": mint,
                                                      "rawTokenAmount": {"tokenAmount": str(int(-s * amt)),
                                                                         "decimals": 0}}]}]}


class FakeHelius:
    def __init__(self, by_addr):
        self.by_addr = by_addr  # address -> txs
        self.calls = []

    def window(self, address, gte, lte, max_pages=10):
        self.calls.append((address, gte, lte))
        txs = [t for t in self.by_addr.get(address, []) if gte <= t["timestamp"] <= lte]
        return sorted(txs, key=lambda t: -t["timestamp"])[: max_pages * 100], len(txs) <= max_pages * 100


class FakeGecko:
    def __init__(self, con):
        self.con = con

    def top_pool(self, mint):
        return "pool_" + mint


def test_hunt_keeps_repeat_early_holders_only():
    con = db.connect(":memory:")
    since, cutoff = 0, 40 * 86400
    prices = [1.0] * 48 + [1.0 + 0.1 * i for i in range(1, 71)] + [8.0] * 200
    mints = {"mintA": None, "mintB": None, "mintC": None}
    for m, t0 in (("mintA", 0), ("mintB", 10 * 86400), ("mintC", 37 * 86400)):  # C runs past the cutoff
        rows = [("pool_" + m, m, ts, o, h, l, c, v) for ts, o, h, l, c, v in hourly(prices, t0)]
        con.executemany("INSERT INTO hour_candles VALUES(?,?,?,?,?,?,?,?)", rows)
        # cover the whole range so nothing is fetched
        con.execute("INSERT OR IGNORE INTO hour_candles VALUES(?,?,?,1,1,1,1,1)", ("pool_" + m, m, 0))
        con.execute("INSERT OR IGNORE INTO hour_candles VALUES(?,?,?,1,1,1,1,1)", ("pool_" + m, m, 60 * 86400))
    early_a, early_b = 48 * H, 10 * 86400 + 48 * H
    pools = {
        "pool_mintA": [swap("a1", "holder", "mintA", early_a, "buy"), swap("a2", "flipper", "mintA", early_a, "buy"),
                       swap("a3", "once", "mintA", early_a, "buy"), swap("a4", "late", "mintA", early_a + 40 * H, "buy")],
        "pool_mintB": [swap("b1", "holder", "mintB", early_b, "buy"), swap("b2", "flipper", "mintB", early_b, "buy"),
                       swap("b4", "late", "mintB", early_b + 40 * H, "buy")],
    }
    wallets = {
        "holder": [swap("h1", "holder", "mintA", early_a + 5 * H, "sell")],           # sold after 5h: held
        "flipper": [swap("f1", "flipper", "mintA", early_a + 600, "sell")],           # sold after 10 min
    }
    h = FakeHelius({**pools, **wallets})
    found, runs = hunt(con, FakeGecko(con), h, mints, since, cutoff, log=lambda *a: None)
    assert {r["mint"] for r in runs} == {"mintA", "mintB"}
    assert [f["wallet"] for f in found] == ["holder"]
    assert all(lte <= cutoff for _, _, lte in h.calls)  # never looked past the cutoff
    assert con.execute("SELECT value FROM hunt_meta WHERE key='cutoff'").fetchone()[0] == cutoff


def test_backtest_refuses_when_test_window_overlaps_hunt_data():
    con = db.connect(":memory:")
    db.insert_trades(con, [
        {"sig": "s1", "wallet": "w", "mint": "m", "ts": 0, "side": "buy", "token_amt": 1, "sol_amt": 1, "source": ""},
        {"sig": "s2", "wallet": "w", "mint": "m", "ts": 100 * 86400, "side": "sell", "token_amt": 1, "sol_amt": 2,
         "source": ""}])
    con.execute("INSERT INTO hunt_meta VALUES('cutoff', ?)", (90 * 86400,))  # split will be at day 60
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    with pytest.raises(SystemExit, match="not be out-of-sample"):
        walk_forward(con, cfg, None, log=lambda *a: None)
