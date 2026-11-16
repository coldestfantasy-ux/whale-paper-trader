# Whale Paper Trader (Solana)

Find Solana wallets that are actually worth copying, prove it on data they weren't
picked on, then copy them live with fake SOL using real executable prices.

No real money is ever spent. Nothing here signs or sends a transaction.

## How it works

```
wallets.txt ──collect──▶ swap history ──backtest──▶ follow.txt ──live──▶ paper account ──report
 (candidates)            (Helius)        score on first 60%,             real Jupiter quotes,
                                         copy-test on last 40%           exits, rug write-offs
```

1. **collect** pulls ~30 days of swaps for each candidate wallet and turns them into
   buy/sell records (works for Pump.fun, Raydium, Jupiter, Meteora, etc.).
2. **backtest** is a walk-forward test:
   - It scores wallets **only on the first 60% of the history**. A wallet needs at least
     10 closed round-trips, profit factor ≥ 1.5, win rate ≥ 35%, no single trade worth
     more than 50% of its profit (filters out lucky one-hit wallets), and a median hold
     of at least 3 minutes (filters out bots you can't copy).
   - It then **copies the top 15 wallets on the last 40%** using minute candles: 20s
     latency, 1.5% slippage per side, fees, a 5% failed-tx rate, a max of 5 open
     positions, and exits on +50% TP, -25% SL, a 4h time stop, or when the whale has
     sold half its position.
   - It prints the copy result next to the whales' own result over the same period,
     plus a sensitivity table for latency and slippage, then writes `follow.txt`.
3. **live** listens to the wallets in `follow.txt` over a websocket. When one buys, it
   waits a simulated execution delay and asks Jupiter for a **real quote for your exact
   size**, so price impact on thin tokens is real. It then books the fill (minus a
   haircut and fees) into the paper account. Open positions are re-quoted on the sell
   side every 15s. If no sell route exists for 15 minutes, the position is written off
   to 0 as a rug.
4. **report** shows equity, win rate, exit reasons and PnL per wallet. Everything is in
   SOL, so a positive result means you beat simply holding SOL.

## Setup (about 10 minutes)

```bash
cd whale-paper-trader
python3 -m venv .venv && source .venv/bin/activate     # Python 3.11+
pip install -r requirements.txt
cp .env.example .env              # paste your keys
cp settings.example.toml settings.toml
cp wallets.example.txt wallets.txt
python -m wt demo                 # sanity check on synthetic data, no keys needed
```

Keys (both free):
- `HELIUS_API_KEY` from https://dashboard.helius.dev
- `JUP_API_KEY` from https://portal.jup.ag. This is optional, but without it the code
  falls back to Jupiter's keyless lite API, which has lower limits and is being retired.

## Finding candidate wallets

Put 50–200 addresses in `wallets.txt`. More candidates means a better chance a real one
survives the filters. Good sources:
- GMGN.ai or Birdeye "top traders" tabs on tokens that ran in the last few weeks
- Dexscreener → a token → "Top Traders"
- Wallets that show up early on several different winners

Don't pre-filter by eye. Let the backtest reject them.

## Daily use

```bash
python -m wt collect        # refresh history (re-run weekly)
python -m wt backtest       # add --baseline to also test "copy everyone" (slow)
python -m wt live           # leave running; Ctrl+C to stop, restarts resume positions
python -m wt report         # check results any time (in a second terminal)
python -m wt reset-paper    # start a fresh paper account
```

Run `live` for **at least 2–4 weeks and 50+ closed trades** before drawing conclusions.
A good week means nothing on its own.

## Settings worth tuning (`settings.toml`)

| Setting | Default | Why |
|---|---|---|
| `live.confluence_wallets` | 1 | Set to 2–3 to only buy when several followed wallets buy the same token. Fewer trades, usually better ones. |
| `live.size_sol` | 0.5 | Larger sizes get worse fills on thin tokens. The Jupiter quote reflects that. |
| `backtest.delay_sec` | 20 | Set it to what your setup can actually achieve. Check the sensitivity table. |
| `scoring.min_median_hold_min` | 3 | Lower it and you start following bots you can't copy. |
| `*.take_profit` / `stop_loss` / `trailing_stop` | 50% / 25% / off | Try a trailing stop of around 0.2. |

## What's realistic and what isn't

Modeled: detection and execution latency, real price impact (live), slippage, network and
priority fees, failed transactions, max concurrent positions, rugs with no exit route,
and whales exiting before you.

Not modeled (assume real results come in a bit **worse**):
- MEV sandwiching on your buys
- Token-2022 transfer taxes and honeypot tokens that quote fine but block sells
- In the backtest, intra-minute price detail. Candles are 1 minute, so fills are
  interpolated and same-candle stop-loss/take-profit conflicts are resolved pessimistically.
- In the backtest, survivorship: candidate wallets were picked today, from today's
  "top traders" lists.

## Costs and limits

- Backtest prices come from each token's busiest **SOL-paired** pool and are quoted in SOL.
  Tokens with no SOL pool are skipped (counted as "no price data").
- GeckoTerminal (backtest prices) allows 30 calls a minute on the free tier and only the
  last ~6 months of history. A backtest with a few hundred signals takes 10–20 minutes the
  first time. Candles are cached in `data/whales.db` after that.
- Helius: `collect` uses the Enhanced Transactions API (credits per call), and `live` uses
  one websocket plus one parse call per whale transaction. Check your usage on the Helius
  dashboard after the first day.

## Files

```
wt/parse.py     Helius tx -> buy/sell (balance-change based, SOL-quoted only)
wt/score.py     round-trips per token, wallet metrics and filters
wt/backtest.py  walk-forward copy simulation
wt/live.py      websocket listener + paper execution
wt/clients.py   Helius / GeckoTerminal / Jupiter
wt/demo.py      synthetic market for testing the logic
tests/          pytest -q
```

Not financial advice. This is a research tool for finding out whether an edge exists
before any real money is involved.
