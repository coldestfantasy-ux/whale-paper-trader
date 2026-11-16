"""Walk-forward copy-trading backtest.

1. Score wallets using only the first `train_frac` of the history.
2. Pick the top N that pass the filters.
3. Replay their *later* buys as if we copied them: entry `delay_sec` after the
   whale, slippage + fees + failed txs, exits by TP / SL / trailing / time /
   whale selling half.
So a wallet only counts as good if it keeps being good on data it wasn't picked on.
"""
import math
import random

from .score import build_trips, group, score_all


def _interp(c, t):
    ts, o, cl = c[0], c[1], c[4]
    f = min(max((t - ts) / 60.0, 0.0), 1.0)
    return o + (cl - o) * f


def simulate_trade(sig_ts, whale_exit_ts, candles, p):
    """Return outcome dict or None if there's no price data around the entry."""
    if not candles:
        return None
    slip = p["slippage_bps"] / 1e4
    delay = p["delay_sec"]
    entry_t = sig_ts + delay
    idx = next((i for i, c in enumerate(candles) if c[0] + 60 > entry_t), None)
    if idx is None or candles[idx][0] > entry_t + 300:
        return None
    c0 = candles[idx]
    fill_t = max(entry_t, c0[0])
    raw_entry = _interp(c0, fill_t)
    if raw_entry <= 0:
        return None
    entry = raw_entry * (1 + slip)
    tp = entry * (1 + p["take_profit"])
    sl = entry * (1 - p["stop_loss"])
    trail = p.get("trailing_stop") or 0.0
    deadline = fill_t + p["max_hold_min"] * 60
    whale_t = (whale_exit_ts + delay) if (p["follow_whale_exit"] and whale_exit_ts) else math.inf
    peak = raw_entry

    def done(px, t, why):
        ex = max(px, 0.0) * (1 - slip)
        return {"entry_ts": fill_t, "exit_ts": t, "entry_px": entry, "exit_px": ex,
                "mult": ex / entry, "reason": why}

    for c in candles[idx + 1:]:
        ts, o, h, l, cl = c[:5]
        if ts >= deadline:
            return done(o, ts, "max_hold")
        stop = max(sl, peak * (1 - trail)) if trail else sl
        if l <= stop:  # assume the bad thing happened first inside the minute
            return done(min(o, stop), ts + 30, "trailing" if trail and stop > sl else "stop_loss")
        if ts + 60 > whale_t:
            t = max(ts, whale_t)
            return done(_interp(c, t), t, "whale_exit")
        if h >= tp:
            return done(max(o, tp), ts + 30, "take_profit")
        peak = max(peak, h)
    last = candles[-1]
    return done(last[4], last[0] + 60, "data_end")


def run_portfolio(outcomes, p):
    rng = random.Random(p.get("seed", 7))
    cash = p["start_sol"]
    fee = p["fee_sol"]
    open_pos, closed = [], []
    stats = {"skipped_full": 0, "skipped_cash": 0, "failed_tx": 0, "skipped_dupe": 0}
    equity_pts = [cash]

    def release(until):
        nonlocal cash
        open_pos.sort(key=lambda x: x["exit_ts"])
        while open_pos and open_pos[0]["exit_ts"] <= until:
            pos = open_pos.pop(0)
            cash += pos["proceeds"]
            closed.append(pos)
            equity_pts.append(cash + sum(x["size"] for x in open_pos))

    for o in sorted(outcomes, key=lambda x: x["entry_ts"]):
        release(o["entry_ts"])
        if any(x["mint"] == o["mint"] for x in open_pos):
            stats["skipped_dupe"] += 1
            continue
        if len(open_pos) >= p["max_open"]:
            stats["skipped_full"] += 1
            continue
        size = min(p["size_sol"], cash - 2 * fee)
        if size < 0.05:
            stats["skipped_cash"] += 1
            continue
        if rng.random() < p["fail_rate"]:
            cash -= fee
            stats["failed_tx"] += 1
            continue
        cash -= size
        proceeds = max((size - fee) * o["mult"] - fee, 0.0)
        open_pos.append({**o, "size": size, "proceeds": proceeds, "pnl": proceeds - size})
    release(math.inf)

    peak, mdd = -math.inf, 0.0
    for e in equity_pts:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak > 0 else 0)
    n = len(closed)
    wins = [c for c in closed if c["pnl"] > 0]
    res = {
        "trades": n, "end_sol": cash, "pnl_sol": cash - p["start_sol"],
        "return": cash / p["start_sol"] - 1, "win_rate": len(wins) / n if n else 0.0,
        "avg_win": sum(c["pnl"] for c in wins) / len(wins) if wins else 0.0,
        "avg_loss": (sum(c["pnl"] for c in closed if c["pnl"] <= 0) / (n - len(wins))) if n > len(wins) else 0.0,
        "max_drawdown": mdd, "closed": closed, **stats,
    }
    reasons = {}
    for c in closed:
        reasons[c["reason"]] = reasons.get(c["reason"], 0) + 1
    res["exit_reasons"] = reasons
    return res


def collect_signals(trips_by_wallet, wallets, start_ts):
    sigs = []
    for w in wallets:
        for tr in trips_by_wallet[w]:
            if tr["open_ts"] >= start_ts:
                sigs.append({"wallet": w, "mint": tr["mint"], "ts": tr["open_ts"],
                             "whale_exit_ts": tr["half_exit_ts"], "whale_sol": tr["first_buy_sol"],
                             "whale_pnl": tr.get("pnl"), "whale_closed": tr["closed"]})
    return sorted(sigs, key=lambda s: s["ts"])


def walk_forward(con, cfg, candle_src, log=print, baseline=False):
    p, rules = cfg["backtest"], cfg["scoring"]
    trades = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY ts")]
    if not trades:
        raise SystemExit("No trades in the database. Run `collect` first.")
    g = group(trades)
    t0, t1 = trades[0]["ts"], trades[-1]["ts"]
    split = int(t0 + (t1 - t0) * p["train_frac"])
    days = lambda a, b: (b - a) / 86400
    log(f"History: {len(trades)} swaps, {len(g)} wallets, {days(t0, t1):.1f} days. "
        f"Scoring on first {days(t0, split):.1f}d, testing on last {days(split, t1):.1f}d.\n")

    scores = score_all(g, rules, before_ts=split)
    ranked = sorted(scores.items(), key=lambda kv: (kv[1]["passes"], kv[1]["score"]), reverse=True)
    selected = [w for w, m in ranked if m["passes"]][: p["top_n"]]
    print_scores(ranked, selected, log)
    if not selected:
        log("\nNo wallet passed the filters in the training window. Add more candidate wallets or loosen [scoring].")
        return None

    trips = {w: build_trips(ts) for w, ts in g.items()}
    signals = collect_signals(trips, selected, split)
    log(f"\n{len(signals)} copy signals from {len(selected)} followed wallets in the test window.")
    max_delay = max(p["delay_sec"], 60)
    cache = {}
    for i, s in enumerate(signals, 1):
        key = (s["mint"], s["ts"])
        if key not in cache:
            cache[key] = candle_src.candles(s["mint"], s["ts"] - 60,
                                            s["ts"] + max_delay + p["max_hold_min"] * 60 + 120)
        if i % 25 == 0:
            log(f"  price data {i}/{len(signals)}")

    def outcomes_for(params, sigs):
        out, missing = [], 0
        for s in sigs:
            o = simulate_trade(s["ts"], s["whale_exit_ts"], cache.get((s["mint"], s["ts"])), params)
            if o is None:
                missing += 1
                continue
            out.append({**o, "wallet": s["wallet"], "mint": s["mint"]})
        return out, missing

    outs, missing = outcomes_for(p, signals)
    res = run_portfolio(outs, p)
    log(f"  ({missing} signals had no price data and were skipped)\n")
    print_result("COPY-TRADING RESULT (out-of-sample, fake money)", res, p, log)

    # whale's own out-of-sample performance vs ours
    wh = [s for s in signals if s["whale_closed"]]
    if wh:
        roi = sum(s["whale_pnl"] for s in wh) / max(sum(s["whale_sol"] for s in wh), 1e-9)
        log(f"\nFor comparison, the whales' own closed trades in the same window: "
            f"{sum(1 for s in wh if s['whale_pnl'] > 0) / len(wh):.0%} win rate, ~{roi:+.0%} on capital.")
        log("The gap between their result and yours is what latency, slippage and fees cost you.")

    log("\nSensitivity (same signals, different realism settings):")
    log(f"  {'delay':>6} {'slippage':>9} {'trades':>7} {'return':>8} {'win%':>6} {'maxDD':>6}")
    for d in sorted({5, p["delay_sec"], 60}):
        for sb in (p["slippage_bps"], p["slippage_bps"] * 2):
            q = {**p, "delay_sec": d, "slippage_bps": sb}
            r = run_portfolio(outcomes_for(q, signals)[0], q)
            log(f"  {d:>5}s {sb:>7}bp {r['trades']:>7} {r['return']:>+8.1%} "
                f"{r['win_rate']:>6.0%} {r['max_drawdown']:>6.0%}")

    by_w = {}
    for c in res["closed"]:
        b = by_w.setdefault(c["wallet"], [0, 0.0])
        b[0] += 1
        b[1] += c["pnl"]
    log("\nPer followed wallet (test window):")
    for w, (n, pnl) in sorted(by_w.items(), key=lambda kv: -kv[1][1]):
        log(f"  {w[:6]}…{w[-4:]}  {n:>3} trades  {pnl:+.3f} SOL")

    if baseline:
        all_sigs = collect_signals(trips, list(g), split)
        for s in all_sigs:
            key = (s["mint"], s["ts"])
            if key not in cache:
                cache[key] = candle_src.candles(s["mint"], s["ts"] - 60,
                                                s["ts"] + max_delay + p["max_hold_min"] * 60 + 120)
        b = run_portfolio(outcomes_for(p, all_sigs)[0], p)
        log("")
        print_result("BASELINE: copy every candidate wallet, no filtering", b, p, log)

    return {"selected": selected, "result": res, "scores": scores}


def fmt_w(w):
    return f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w


def print_scores(ranked, selected, log):
    log(f"{'wallet':<12} {'trips':>5} {'win%':>5} {'PF':>5} {'PnL SOL':>8} {'hold':>6}  verdict")
    for w, m in ranked[:30]:
        if m["n_trips"] == 0:
            continue
        verdict = "FOLLOW" if w in selected else ("pass (not top N)" if m["passes"] else "; ".join(m["reasons"]))
        log(f"{fmt_w(w):<12} {m['n_trips']:>5} {m['win_rate']:>5.0%} {min(m['profit_factor'], 99):>5.1f} "
            f"{m['pnl_sol']:>8.2f} {m['median_hold_min']:>5.0f}m  {verdict}")


def print_result(title, r, p, log):
    log(f"=== {title} ===")
    log(f"Start {p['start_sol']:.2f} SOL -> end {r['end_sol']:.2f} SOL  ({r['return']:+.1%})")
    log(f"Trades {r['trades']}, win rate {r['win_rate']:.0%}, avg win {r['avg_win']:+.3f}, "
        f"avg loss {r['avg_loss']:+.3f} SOL, max drawdown {r['max_drawdown']:.0%}")
    log(f"Exits: {r['exit_reasons']}")
    log(f"Skipped: {r['skipped_full']} (max positions), {r['skipped_dupe']} (already holding), "
        f"{r['skipped_cash']} (no cash); failed txs: {r['failed_tx']}")
    log("Return is in SOL terms, so anything above 0% beat just holding SOL.")


def write_follow(path, wallets, scores):
    lines = ["# Wallets selected by the last backtest. Edit freely; one per line.\n"]
    for w in wallets:
        m = scores[w]
        lines.append(f"{w}  # trips={m['n_trips']} win={m['win_rate']:.0%} PF={min(m['profit_factor'], 99):.1f}\n")
    with open(path, "w") as f:
        f.writelines(lines)
