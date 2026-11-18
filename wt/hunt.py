"""Find wallets by what they did on tokens that ran, not by today's leaderboards.

1. Tokens that rose `min_mult`-`max_mult`x (default 5-10x, close to close on hourly
   candles) over at least `min_run_hours` and at most `max_run_days`.
2. Wallets that bought each one early: between the bottom of the run and the first
   hour it closed at `early_mult`x the bottom (capped at `early_max_hours`).
3. Of those, wallets that held for at least `hold_hours` (no sell of that token in
   that time), on at least `min_tokens` different tokens.

Everything used here ends before `cutoff`, the start of the backtest's out-of-sample
window. The cutoff is saved in the database, and `backtest` refuses to call a result
out-of-sample if its test window starts before it.
"""
import time
from collections import defaultdict, deque

from .clients import hour_candles
from .config import STABLES, WSOL
from .parse import parse_tx

DEFAULTS = {
    "min_mult": 5.0, "max_mult": 10.0, "min_run_hours": 24, "max_run_days": 14,
    "early_mult": 2.0, "early_max_hours": 24, "hold_hours": 2.0, "min_tokens": 2,
    "min_buy_sol": 0.2, "pool_pages": 40, "hold_pages": 3,
}


def find_run(candles, min_mult, max_mult, min_run_hours, max_run_days, early_mult, early_max_hours):
    """Largest close-to-close rise in hourly candles whose low comes at least
    min_run_hours and at most max_run_days before the high. Returns the run only if its
    multiple is within [min_mult, max_mult] (a 30x token is not a 5-10x token)."""
    closes = [(c[0], c[4]) for c in candles if c[4] > 0]
    best = None
    lo_gap, hi_gap = min_run_hours * 3600, max_run_days * 86400
    lows, k = deque(), 0  # indices of candidate lows, closes increasing (sliding minimum)
    for tj, cj in closes:
        while k < len(closes) and closes[k][0] <= tj - lo_gap:
            while lows and closes[lows[-1]][1] >= closes[k][1]:
                lows.pop()
            lows.append(k)
            k += 1
        while lows and closes[lows[0]][0] < tj - hi_gap:
            lows.popleft()
        if lows:
            ti, ci = closes[lows[0]]
            if best is None or cj / ci > best["mult"]:
                best = {"low_ts": ti, "low": ci, "high_ts": tj, "high": cj, "mult": cj / ci}
    if not best or not (min_mult <= best["mult"] <= max_mult):
        return None
    end = best["low_ts"] + early_max_hours * 3600
    hit = next((t for t, c in closes if best["low_ts"] < t <= end and c >= early_mult * best["low"]), None)
    best["early_end_ts"] = (hit + 3600) if hit else end  # candle ts is the hour's start
    return best


def early_buyers(helius, pool, mint, start_ts, end_ts, min_buy_sol, max_pages, chunk=3600):
    """First buy of `mint` per wallet in [start, end], walking forward an hour at a time so
    the earliest buyers are seen first even if the page budget runs out."""
    buyers, pages_left, complete, t = {}, max_pages, True, start_ts
    while t < end_ts and pages_left > 0:
        txs, ok = helius.window(pool, t, min(t + chunk, end_ts), min(10, pages_left))
        pages_left -= max(1, -(-len(txs) // 100))
        complete &= ok
        for tx in txs:
            w = tx.get("feePayer")
            tr = parse_tx(tx, w, min_buy_sol) if w else None
            if tr and tr["mint"] == mint and tr["side"] == "buy":
                if w not in buyers or tr["ts"] < buyers[w]["ts"]:
                    buyers[w] = tr
        t += chunk
    return buyers, complete and t >= end_ts


def held(helius, wallet, mint, buy_ts, hold_hours, max_pages):
    """True: no sell of `mint` within hold_hours of buy_ts. False: sold sooner.
    None: too many transactions to check (a bot) or the history couldn't be read."""
    try:
        txs, complete = helius.window(wallet, buy_ts, buy_ts + hold_hours * 3600, max_pages)
    except RuntimeError:
        return None
    for tx in txs:
        tr = parse_tx(tx, wallet, 0.0)
        if tr and tr["mint"] == mint and tr["side"] == "sell" and tr["ts"] > buy_ts:
            return False
    return True if complete else None


def candidate_mints(con, gecko, extra_dbs=(), pages=5, log=print):
    from .db import connect
    from .discover import list_pools
    mints = {}
    for path in extra_dbs:
        for (m,) in connect(path).execute("SELECT DISTINCT mint FROM trades"):
            mints.setdefault(m, None)
    for pool, mint, _ in list_pools(gecko, pages, log):
        mints.setdefault(mint, pool)
    return {m: p for m, p in mints.items() if m not in STABLES and m != WSOL}


def hunt(con, gecko, helius, mints, since, cutoff, p=None, log=print):
    p = {**DEFAULTS, **(p or {})}
    latest = 0  # newest timestamp of any data the selection looked at
    runs = []
    log(f"Scanning {len(mints)} tokens for {p['min_mult']:g}-{p['max_mult']:g}x runs between "
        f"{time.strftime('%Y-%m-%d', time.gmtime(since))} and {time.strftime('%Y-%m-%d', time.gmtime(cutoff))}...")
    # the run and the hold checks after it must all end before the cutoff
    last_high = cutoff - p["hold_hours"] * 3600 - p["early_max_hours"] * 3600
    for i, (mint, pool) in enumerate(mints.items(), 1):
        pool = pool or gecko.top_pool(mint)
        if pool:
            c = hour_candles(gecko, pool, mint, since, last_high)
            r = find_run([x for x in c if x[0] <= last_high], p["min_mult"], p["max_mult"],
                         p["min_run_hours"], p["max_run_days"], p["early_mult"], p["early_max_hours"])
            if r:
                runs.append({**r, "mint": mint, "pool": pool})
        if i % 25 == 0:
            log(f"  {i}/{len(mints)} tokens checked, {len(runs)} runs found")
    log(f"{len(runs)} tokens had a {p['min_mult']:g}-{p['max_mult']:g}x run.")

    early = defaultdict(dict)  # wallet -> mint -> buy record
    for k, r in enumerate(runs, 1):
        b, complete = early_buyers(helius, r["pool"], r["mint"], r["low_ts"] - 3600, r["early_end_ts"],
                                   p["min_buy_sol"], p["pool_pages"])
        latest = max(latest, r["high_ts"] + 3600, r["early_end_ts"])
        for w, tr in b.items():
            early[w][r["mint"]] = tr
        log(f"  [{k}/{len(runs)}] {r['mint'][:6]}… {r['mult']:.1f}x over "
            f"{(r['high_ts'] - r['low_ts']) / 86400:.1f}d: {len(b)} early buyers"
            f"{'' if complete else ' (page budget ran out; earliest hours only)'}")

    repeat = {w: m for w, m in early.items() if len(m) >= p["min_tokens"]}
    log(f"{len(repeat)} wallets bought early on {p['min_tokens']}+ of these tokens; checking hold times...")
    out = []
    for w, by_mint in repeat.items():
        kept, bot = [], False
        for mint, tr in by_mint.items():
            h = held(helius, w, mint, tr["ts"], p["hold_hours"], p["hold_pages"])
            latest = max(latest, tr["ts"] + int(p["hold_hours"] * 3600))
            if h is None:
                bot = True
                break
            if h:
                kept.append(tr)
        if not bot and len({t["mint"] for t in kept}) >= p["min_tokens"]:
            out.append({"wallet": w, "hits": sorted(kept, key=lambda t: t["ts"])})
    out.sort(key=lambda x: -len(x["hits"]))
    if latest > cutoff:
        raise RuntimeError("hunt looked at data after the cutoff")  # guarded above; never expected
    con.execute("INSERT OR REPLACE INTO hunt_meta VALUES('cutoff', ?)", (int(cutoff),))
    con.commit()
    log(f"{len(out)} wallets bought early and held {p['hold_hours']:g}h+ on {p['min_tokens']}+ tokens.")
    return out, runs


def write_wallets(path, found, runs):
    mult = {r["mint"]: r["mult"] for r in runs}
    lines = ["# From `python -m wt hunt`: bought early into a 5-10x run and held, more than once.\n"]
    for f in found:
        hits = ", ".join(f"{t['mint'][:6]}({mult.get(t['mint'], 0):.0f}x)" for t in f["hits"])
        lines.append(f"{f['wallet']}  # {len(f['hits'])} tokens: {hits}\n")
    with open(path, "w") as fh:
        fh.writelines(lines)
