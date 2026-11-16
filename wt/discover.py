"""Find candidate wallets without any API key.

Scans GeckoTerminal's trending and top-volume Solana pools, pulls each pool's larger
recent trades (last 24h) and collects the wallets behind them. This only produces
*candidates* for wallets.txt; whether any of them is worth copying is decided by
`collect` + `backtest`, never here.
"""
from collections import defaultdict

from .clients import Gecko, _get, _other_side
from .config import STABLES, WSOL


def list_pools(g, pages=3, log=print):
    """SOL-paired pools from trending + top 24h volume, de-duplicated. -> [(pool, mint, name)]"""
    seen, out = set(), []
    for path, params in (("trending_pools", {}), ("pools", {"sort": "h24_volume_usd_desc"})):
        for page in range(1, pages + 1):
            try:
                r = _get(g.s, f"{g.BASE}/networks/solana/{path}", g.lim, params={**params, "page": page})
            except RuntimeError:
                break
            if r.status_code != 200:
                break
            for p in r.json().get("data") or []:
                rel = p.get("relationships") or {}
                ids = [((rel.get(k) or {}).get("data") or {}).get("id", "").split("_", 1)[-1]
                       for k in ("base_token", "quote_token")]
                if WSOL not in ids:
                    continue
                mint = ids[0] if ids[1] == WSOL else ids[1]
                addr = p["attributes"]["address"]
                if mint in STABLES or mint == WSOL or _other_side(p, mint) != WSOL or addr in seen:
                    continue
                seen.add(addr)
                out.append((addr, mint, p["attributes"].get("name", "")))
    log(f"{len(out)} SOL-paired pools to scan")
    return out


def pool_trades(g, pool, min_usd):
    try:
        r = _get(g.s, f"{g.BASE}/networks/solana/pools/{pool}/trades", g.lim,
                 params={"trade_volume_in_usd_greater_than": min_usd})
    except RuntimeError:
        return []  # still rate-limited after retries: skip this pool
    if r.status_code != 200:
        return []
    return [t["attributes"] for t in r.json().get("data") or []]


def rank_wallets(trades_by_pool, max_trades=12):
    """trades_by_pool: {mint: [gecko trade attrs]}. Returns candidates, best first.
    Deliberately loose: drops wallets that never bought and ones with many trades in the
    sample (on real data those are almost all bots), then orders by size bought."""
    w = defaultdict(lambda: {"trades": 0, "buy_sol": 0.0, "sell_sol": 0.0, "bought": set(), "closed_pnl": 0.0})
    for mint, trades in trades_by_pool.items():
        per = defaultdict(lambda: [0.0, 0.0])
        for t in trades:
            if t.get("is_anomalous"):
                continue
            a = w[t["tx_from_address"]]
            a["trades"] += 1
            if t["kind"] == "buy" and t["from_token_address"] == WSOL:
                amt = float(t["from_token_amount"])
                a["buy_sol"] += amt
                a["bought"].add(mint)
                per[t["tx_from_address"]][0] += amt
            elif t["kind"] == "sell" and t["to_token_address"] == WSOL:
                amt = float(t["to_token_amount"])
                a["sell_sol"] += amt
                per[t["tx_from_address"]][1] += amt
        for addr, (b, s) in per.items():
            if b > 0 and s > 0:
                w[addr]["closed_pnl"] += s - b
    out = []
    for addr, a in w.items():
        if not a["bought"] or a["trades"] > max_trades:
            continue
        out.append({"wallet": addr, "tokens": len(a["bought"]), "trades": a["trades"],
                    "buy_sol": a["buy_sol"], "sell_sol": a["sell_sol"], "closed_pnl": a["closed_pnl"]})
    out.sort(key=lambda x: (x["closed_pnl"] > 0, x["buy_sol"]), reverse=True)
    return out


def human_paced(cands, limit, max_tx_per_day, rpc, log=print, max_probes=600):
    """Keep candidates whose on-chain activity a person could follow. One cheap RPC call
    each: the time span of the wallet's latest 1000 transactions gives its pace."""
    import time
    keep, now = [], time.time()
    for i, c in enumerate(cands[:max_probes], 1):
        try:
            page = rpc.call("getSignaturesForAddress", [c["wallet"], {"limit": 1000}]) or []
        except RuntimeError:
            continue
        if page:
            days = max((now - (page[-1].get("blockTime") or now)) / 86400, 0.01)
            c["tx_per_day"] = len(page) / days if len(page) == 1000 else len(page) / max(days, 30)
            if c["tx_per_day"] <= max_tx_per_day:
                keep.append(c)
        if i % 50 == 0:
            log(f"  checked {i} wallets, {len(keep)} human-paced so far")
        if len(keep) >= limit:
            break
    return keep


def discover(con, pages=3, min_usd=300, limit=150, max_tx_per_day=60, log=print, rpc=None):
    g = Gecko(con)
    by_mint = {}
    pools = list_pools(g, pages, log)
    for i, (pool, mint, name) in enumerate(pools, 1):
        by_mint.setdefault(mint, []).extend(pool_trades(g, pool, min_usd))
        if i % 10 == 0:
            log(f"  scanned {i}/{len(pools)} pools")
    ranked = rank_wallets(by_mint)
    if not max_tx_per_day:
        return ranked[:limit]
    if rpc is None:
        from .rpc import SolanaRPC
        rpc = SolanaRPC(per_min=150)
    log(f"{len(ranked)} wallets seen; checking which trade at a pace you could copy "
        f"(<= {max_tx_per_day} txs/day)")
    return human_paced(ranked, limit, max_tx_per_day, rpc, log)


def write_wallets(path, cands):
    lines = ["# Candidates from `python -m wt discover` (GeckoTerminal, last 24h of larger trades).\n",
             "# Unvetted: run collect + backtest to see which, if any, are worth following.\n"]
    for c in cands:
        pace = f" ~{c['tx_per_day']:.0f} txs/day" if "tx_per_day" in c else ""
        lines.append(f"{c['wallet']}  #{pace} tokens={c['tokens']} trades={c['trades']} "
                     f"bought={c['buy_sol']:.1f} SOL 24h-closed-pnl={c['closed_pnl']:+.1f}\n")
    with open(path, "w") as f:
        f.writelines(lines)
