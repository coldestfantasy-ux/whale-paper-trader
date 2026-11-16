"""Turn a Helius enhanced transaction into a simple buy/sell record for one wallet.

Works off balance changes (accountData) rather than DEX-specific decoding, so it
covers Jupiter, Raydium, Pump.fun, Meteora, etc. the same way. Only SOL-quoted
swaps are kept (that's where nearly all memecoin trading happens); swaps that
touch USDC/USDT are skipped so PnL is always measured in SOL.
"""
from collections import defaultdict

from .config import STABLES, WSOL

DUST = 1e-9


def parse_tx(tx, wallet, min_sol=0.01):
    if not tx or tx.get("transactionError"):
        return None
    native = 0
    tokens = defaultdict(float)
    for acc in tx.get("accountData") or []:
        if acc.get("account") == wallet:
            native += acc.get("nativeBalanceChange") or 0
        for tb in acc.get("tokenBalanceChanges") or []:
            if tb.get("userAccount") != wallet:
                continue
            raw = tb.get("rawTokenAmount") or {}
            try:
                amt = int(raw.get("tokenAmount", 0)) / 10 ** int(raw.get("decimals", 0))
            except (TypeError, ValueError):
                continue
            tokens[tb.get("mint")] += amt

    sol = native / 1e9 + tokens.pop(WSOL, 0.0)
    if any(abs(tokens.get(s, 0.0)) > DUST for s in STABLES):
        return None
    moved = {m: a for m, a in tokens.items() if abs(a) > DUST}
    if len(moved) != 1:
        return None
    mint, amt = next(iter(moved.items()))

    if amt > 0 and sol < -min_sol:
        side = "buy"
    elif amt < 0 and sol > min_sol:
        side = "sell"
    else:
        return None  # transfer, airdrop, LP action, etc.
    return {
        "sig": tx.get("signature"),
        "wallet": wallet,
        "mint": mint,
        "ts": int(tx.get("timestamp") or 0),
        "side": side,
        "token_amt": abs(amt),
        "sol_amt": abs(sol),
        "source": tx.get("source") or "",
    }
