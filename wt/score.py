"""Group a wallet's swaps into round-trips per token and score the wallet."""
import math
import statistics
from collections import defaultdict


def build_trips(trades):
    """trades: one wallet's buy/sell records sorted by ts.
    Returns list of trips: a position opened from zero and (maybe) closed."""
    open_by_mint = {}
    trips = []
    for t in sorted(trades, key=lambda x: (x["ts"], x["sig"])):
        m = t["mint"]
        tr = open_by_mint.get(m)
        if t["side"] == "buy":
            if tr is None:
                tr = {"wallet": t["wallet"], "mint": m, "open_ts": t["ts"], "open_sig": t["sig"],
                      "first_buy_sol": t["sol_amt"], "qty": 0.0, "bought": 0.0, "sold": 0.0,
                      "cost": 0.0, "proceeds": 0.0, "half_exit_ts": None, "close_ts": None,
                      "n_buys": 0, "n_sells": 0}
                open_by_mint[m] = tr
                trips.append(tr)
            tr["qty"] += t["token_amt"]
            tr["bought"] += t["token_amt"]
            tr["cost"] += t["sol_amt"]
            tr["n_buys"] += 1
        else:
            if tr is None:
                continue  # selling something bought before our window
            amt = min(t["token_amt"], tr["qty"])
            if t["token_amt"] <= 0:
                continue
            tr["proceeds"] += t["sol_amt"] * amt / t["token_amt"]
            tr["qty"] -= amt
            tr["sold"] += amt
            tr["n_sells"] += 1
            if tr["half_exit_ts"] is None and tr["sold"] >= 0.5 * tr["bought"]:
                tr["half_exit_ts"] = t["ts"]
            if tr["qty"] <= 0.01 * tr["bought"]:
                tr["close_ts"] = t["ts"]
                del open_by_mint[m]
    for tr in trips:
        tr["closed"] = tr["close_ts"] is not None
        if tr["closed"]:
            tr["pnl"] = tr["proceeds"] - tr["cost"]
            tr["roi"] = tr["pnl"] / tr["cost"] if tr["cost"] else 0.0
            tr["hold_min"] = (tr["close_ts"] - tr["open_ts"]) / 60
    return trips


def score_wallet(trips, rules):
    closed = [t for t in trips if t["closed"]]
    n = len(closed)
    m = {"n_trips": n, "n_open": len(trips) - n}
    if n == 0:
        m.update(passes=False, reasons=["no closed trips"], score=0.0)
        return m
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [-t["pnl"] for t in closed if t["pnl"] <= 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    m["win_rate"] = len(wins) / n
    m["pnl_sol"] = gross_win - gross_loss
    m["profit_factor"] = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    m["top_trip_share"] = max(wins) / gross_win if gross_win > 0 else 1.0
    m["median_hold_min"] = statistics.median(t["hold_min"] for t in closed)
    m["median_roi"] = statistics.median(t["roi"] for t in closed)
    m["avg_size_sol"] = statistics.mean(t["cost"] for t in closed)

    reasons = []
    if n < rules["min_trips"]:
        reasons.append(f"only {n} trips")
    if m["pnl_sol"] <= 0:
        reasons.append("unprofitable")
    if m["profit_factor"] < rules["min_profit_factor"]:
        reasons.append(f"PF {m['profit_factor']:.2f}")
    if m["win_rate"] < rules["min_win_rate"]:
        reasons.append(f"win rate {m['win_rate']:.0%}")
    if m["top_trip_share"] > rules["max_top_trip_share"]:
        reasons.append(f"one trade = {m['top_trip_share']:.0%} of profit")
    if m["median_hold_min"] < rules["min_median_hold_min"]:
        reasons.append(f"holds {m['median_hold_min']:.1f}m (bot?)")
    if not rules["min_avg_size_sol"] <= m["avg_size_sol"] <= rules["max_avg_size_sol"]:
        reasons.append(f"avg size {m['avg_size_sol']:.2f} SOL")
    m["passes"] = not reasons
    m["reasons"] = reasons
    # simple, transparent ranking score
    m["score"] = (math.log1p(n) * min(m["profit_factor"], 5) * m["win_rate"]
                  * (1 - min(m["top_trip_share"], 0.99)))
    return m


def score_all(trades_by_wallet, rules, before_ts=None):
    """Score each wallet using only trips fully closed before `before_ts` (no look-ahead)."""
    out = {}
    for w, trades in trades_by_wallet.items():
        trips = build_trips(trades)
        if before_ts is not None:
            trips = [t for t in trips if t["open_ts"] < before_ts]
            for t in trips:
                if t["closed"] and t["close_ts"] >= before_ts:
                    t["closed"] = False
        out[w] = score_wallet(trips, rules)
    return out


def group(trades):
    g = defaultdict(list)
    for t in trades:
        g[t["wallet"]].append(t)
    return g
