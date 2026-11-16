"""Usage:
  python -m wt discover           find candidate wallets from trending pools (no key), writes wallets.txt
  python -m wt collect            pull swap history for wallets.txt
  python -m wt backtest [--baseline]   score wallets + out-of-sample copy test, writes follow.txt
  python -m wt live               paper trade follow.txt in real time
  python -m wt report             paper account summary
  python -m wt reset-paper        wipe the paper account and positions
  python -m wt demo               run scoring + backtest on synthetic data (no keys needed)
  python -m wt dashboard [--demo] web dashboard of the paper account (http://127.0.0.1:8050)
"""
import argparse
import asyncio
import sys
import time

from . import config, db


def cmd_discover(cfg, args):
    import os
    from .discover import discover, write_wallets

    path = args.out or cfg["paths"]["wallets"]
    if os.path.exists(path) and config.read_wallets(path) and not args.force:
        raise SystemExit(f"{path} already has wallets. Use --force to replace it.")
    cands = discover(db.connect(cfg["paths"]["db"]), args.pages, args.min_usd, args.limit, args.max_tx_per_day)
    write_wallets(path, cands)
    print(f"Wrote {len(cands)} candidate wallets to {path}. Next: python -m wt collect")


def cmd_collect(cfg, args):
    from .clients import Helius
    from .parse import parse_tx

    wallets = config.read_wallets(cfg["paths"]["wallets"])
    if not wallets:
        raise SystemExit(f"Add candidate wallet addresses to {cfg['paths']['wallets']} (one per line).")
    c = cfg["collect"]
    keyless = not cfg["keys"]["helius"]
    if keyless:
        from .rpc import SolanaRPC, TooActive
        rpc = SolanaRPC()
        print("No HELIUS_API_KEY: using Solana's public RPC (slower; skips bots and very quiet wallets).")
    else:
        h = Helius(cfg["keys"]["helius"])
    con = db.connect(cfg["paths"]["db"])
    since = int(time.time() - c["days"] * 86400)
    fresh = time.time() - c["resume_hours"] * 3600
    done = {r[0] for r in con.execute("SELECT wallet FROM wallet_meta WHERE collected_at>=?", (fresh,))}
    for i, w in enumerate(wallets, 1):
        if w in done:
            continue  # collected recently: lets an interrupted run pick up where it stopped
        if keyless:
            try:
                txs = rpc.history(w, since, c["rpc_max_tx"], c["rpc_min_tx"])
            except TooActive:
                print(f"[{i}/{len(wallets)}] {w[:6]}…  skipped: more than {c['rpc_max_tx']} txs in {c['days']}d (bot)")
                con.execute("INSERT OR REPLACE INTO wallet_meta VALUES(?,?,?,?,?)", (w, 0, 0, 0, int(time.time())))
                con.commit()
                continue
            except RuntimeError as e:
                print(f"[{i}/{len(wallets)}] {w[:6]}…  failed ({e}); will retry next run")
                continue
        else:
            txs = h.history(w, since, cfg["collect"]["max_pages"])
        trades = [t for t in (parse_tx(x, w, cfg["collect"]["min_sol"]) for x in txs) if t]
        db.insert_trades(con, trades)
        ts = [x.get("timestamp", 0) for x in txs] or [0]
        con.execute("INSERT OR REPLACE INTO wallet_meta VALUES(?,?,?,?,?)",
                    (w, max(ts), min(ts), len(txs), int(time.time())))
        con.commit()
        capped = " (hit page cap: very active wallet, probably a bot)" if not keyless and len(txs) >= cfg["collect"]["max_pages"] * 100 * 0.95 else ""
        print(f"[{i}/{len(wallets)}] {w[:6]}…  {len(txs)} txs -> {len(trades)} swaps{capped}")
    print("Done. Next: python -m wt backtest")


def cmd_backtest(cfg, args):
    from .backtest import walk_forward, write_follow
    from .clients import Gecko

    con = db.connect(cfg["paths"]["db"])
    out = walk_forward(con, cfg, Gecko(con), baseline=args.baseline)
    if out:
        write_follow(cfg["live"]["follow_file"], out["selected"], out["scores"])
        print(f"\nWrote {len(out['selected'])} wallets to {cfg['live']['follow_file']}.")


def cmd_live(cfg, args):
    from .clients import Helius, Jupiter
    from .live import run_live

    follow = config.read_wallets(cfg["live"]["follow_file"])
    if not follow:
        raise SystemExit(f"{cfg['live']['follow_file']} is empty. Run backtest first or add wallets by hand.")
    con = db.connect(cfg["paths"]["db"])
    try:
        asyncio.run(run_live(con, cfg, Helius(cfg["keys"]["helius"]), Jupiter(cfg["keys"]["jupiter"]), follow))
    except KeyboardInterrupt:
        print("\nStopped. Positions stay open in the paper account; `live` resumes them.")


def cmd_report(cfg, args):
    from .report import paper_report
    paper_report(db.connect(cfg["paths"]["db"]))


def cmd_reset(cfg, args):
    con = db.connect(cfg["paths"]["db"])
    for t in ("paper_account", "positions", "whale_holdings", "events", "equity_snapshots"):
        con.execute(f"DELETE FROM {t}")
    con.commit()
    print("Paper account reset.")


def cmd_demo(cfg, args):
    from .demo import run_demo
    run_demo(cfg)


def cmd_dashboard(cfg, args):
    from .dashboard import serve
    path = cfg["paths"]["db"]
    if args.demo:
        from .demo import seed_dashboard_demo
        path = str(__import__("pathlib").Path(path).with_name("demo.db"))
        print("Building demo data from the synthetic backtest...")
        seed_dashboard_demo(cfg, path)
    serve(path, args.host, args.port, demo=args.demo)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default="settings.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ds = sub.add_parser("discover", help="find candidate wallets (no API key needed)")
    ds.add_argument("--pages", type=int, default=3, help="pages of trending/top-volume pools to scan")
    ds.add_argument("--min-usd", type=float, default=300, help="only look at trades at least this big")
    ds.add_argument("--limit", type=int, default=150)
    ds.add_argument("--max-tx-per-day", type=float, default=60,
                    help="drop wallets busier than this (bots); 0 = keep everyone, no RPC calls")
    ds.add_argument("--out", help="write here instead of the wallets file in settings")
    ds.add_argument("--force", action="store_true", help="replace an existing wallets.txt")
    sub.add_parser("collect")
    b = sub.add_parser("backtest")
    b.add_argument("--baseline", action="store_true", help="also simulate copying every candidate (slow)")
    sub.add_parser("live")
    sub.add_parser("report")
    sub.add_parser("reset-paper")
    sub.add_parser("demo")
    d = sub.add_parser("dashboard", help="web dashboard of the paper account")
    d.add_argument("--port", type=int, default=8050)
    d.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from another device")
    d.add_argument("--demo", action="store_true", help="show synthetic demo trades instead of your account")
    args = ap.parse_args(argv)
    cfg = config.load(args.settings)
    {"discover": cmd_discover, "collect": cmd_collect, "backtest": cmd_backtest, "live": cmd_live, "report": cmd_report,
     "reset-paper": cmd_reset, "demo": cmd_demo, "dashboard": cmd_dashboard}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
