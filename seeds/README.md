# Seed wallet lists

Public Solana wallet addresses found by `python -m wt discover` and the
human-pace check, saved so a fresh checkout can carry on without redoing
discovery.

- `verified_traders.txt`: wallets that trade at a human pace and make SOL swaps.
  Copy it to `wallets.txt` to collect and backtest them.
- `candidates.txt`: the wider pool those were picked from (most are bots or
  inactive).

Result so far (14 days of history): none passed the scoring filters. The fast
wallets (`CccSh2`, `6661Go`) lose money when copied with a 5-60 s delay. The
ones worth a longer look are `GVg53P` (long holds, few trips) and `6RsDyk`
(profit factor 1.42).
