import time


def paper_report(con, log=print):
    acc = con.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    if not acc:
        log("No paper account yet. Run `live` first.")
        return
    opens = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='open'")]
    closed = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='closed' ORDER BY exit_ts")]
    open_val = sum(p["last_value"] or 0 for p in opens)
    equity = acc["cash_sol"] + open_val
    days = (time.time() - acc["started_at"]) / 86400
    log(f"Paper account since {time.strftime('%Y-%m-%d %H:%M', time.localtime(acc['started_at']))} ({days:.1f} days)")
    log(f"Start {acc['start_sol']:.3f} SOL | cash {acc['cash_sol']:.3f} | open positions {open_val:.3f} "
        f"| equity {equity:.3f} SOL ({equity / acc['start_sol'] - 1:+.1%})")
    if closed:
        pnls = [c["proceeds_sol"] - c["cost_sol"] for c in closed]
        wins = [x for x in pnls if x > 0]
        log(f"Closed trades {len(closed)}, win rate {len(wins) / len(closed):.0%}, "
            f"realized {sum(pnls):+.3f} SOL, best {max(pnls):+.3f}, worst {min(pnls):+.3f}")
        reasons, by_w = {}, {}
        for c, x in zip(closed, pnls):
            reasons[c["exit_reason"]] = reasons.get(c["exit_reason"], 0) + 1
            b = by_w.setdefault(c["wallet"], [0, 0.0])
            b[0] += 1
            b[1] += x
        log(f"Exit reasons: {reasons}")
        log("By wallet:")
        for w, (n, x) in sorted(by_w.items(), key=lambda kv: -kv[1][1]):
            log(f"  {w[:6]}…{w[-4:]}  {n:>3} trades  {x:+.3f} SOL")
    if opens:
        log("Open:")
        for p in opens:
            r = (p["last_value"] or 0) / p["cost_sol"] - 1
            log(f"  {p['mint'][:6]}…  cost {p['cost_sol']:.3f}  now {p['last_value'] or 0:.3f} ({r:+.0%})"
                f"  held {(time.time() - p['entry_ts']) / 60:.0f}m")
    log("Equity is in SOL, so a positive number means you beat simply holding SOL.")
