"""Keyless fallback for `collect`: wallet history from Solana's public RPC.

Slower than Helius (one call per transaction, a few calls a second) but needs no key.
Each raw transaction is reshaped into the Helius "enhanced" fields that parse.parse_tx
reads (accountData / nativeBalanceChange / tokenBalanceChanges), so parsing is shared.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .clients import RateLimiter


MAX_TX_VERSION = 2


class TooActive(Exception):
    """Wallet has more transactions in the window than we are willing to fetch (a bot)."""


def to_enhanced(sig, tx):
    """Raw getTransaction result -> the subset of Helius' enhanced format parse_tx uses."""
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    loaded = meta.get("loadedAddresses") or {}
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg.get("accountKeys") or []]
    keys += (loaded.get("writable") or []) + (loaded.get("readonly") or [])
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    data = [{"account": k, "nativeBalanceChange": post[i] - pre[i], "tokenBalanceChanges": []}
            for i, k in enumerate(keys) if i < len(pre) and i < len(post)]

    bal = {}  # accountIndex -> [owner, mint, decimals, pre, post]
    for side, col in ((meta.get("preTokenBalances") or [], 3), (meta.get("postTokenBalances") or [], 4)):
        for b in side:
            ui = b.get("uiTokenAmount") or {}
            e = bal.setdefault(b.get("accountIndex"), [b.get("owner"), b.get("mint"), int(ui.get("decimals") or 0), 0, 0])
            e[col] = int(ui.get("amount") or 0)
            e[0] = e[0] or b.get("owner")
    changes = [{"userAccount": o, "mint": m, "rawTokenAmount": {"tokenAmount": str(b - a), "decimals": d}}
               for o, m, d, a, b in bal.values() if a != b]
    if data:
        data[0]["tokenBalanceChanges"] = changes
    return {"signature": sig, "timestamp": tx.get("blockTime") or 0, "transactionError": meta.get("err"),
            "accountData": data, "source": "RPC"}


class SolanaRPC:
    def __init__(self, url="https://api.mainnet-beta.solana.com", per_min=60, workers=2):
        self.url = url
        self.local = threading.local()
        self.lim = RateLimiter(per_min)  # the public RPC starts refusing well below its documented limit
        self.workers = workers

    @property
    def s(self):
        if not hasattr(self.local, "s"):
            self.local.s = requests.Session()
        return self.local.s

    def call(self, method, params, tries=12):
        for i in range(tries):
            self.lim.wait()
            try:
                r = self.s.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            except requests.RequestException:
                time.sleep(min(2 ** i, 60))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** i + 1, 60))
                continue
            j = r.json()
            if "error" in j:
                if (j["error"] or {}).get("code") in (429, -32005):  # node-side rate limit
                    time.sleep(min(2 ** i + 1, 60))
                    continue
                return None
            return j.get("result")
        raise RuntimeError(f"RPC {method} failed after {tries} tries")

    def transaction(self, sig):
        """One transaction in enhanced format, or None. Accepts newer transaction versions:
        with a lower cap the node refuses them and they would be silently missing."""
        tx = self.call("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": MAX_TX_VERSION}])
        return to_enhanced(sig, tx) if tx else None

    def signatures(self, wallet, since_ts, max_sigs):
        """Successful signatures newest-first back to since_ts. Raises TooActive past max_sigs."""
        out, before = [], None
        while True:
            opts = {"limit": 1000, **({"before": before} if before else {})}
            page = self.call("getSignaturesForAddress", [wallet, opts]) or []
            ended = False
            for s in page:
                if (s.get("blockTime") or 0) < since_ts:
                    ended = True
                    break
                if not s.get("err"):
                    out.append(s["signature"])
            if len(out) > max_sigs:
                raise TooActive(wallet)
            if ended or len(page) < 1000:
                return out
            before = page[-1]["signature"]

    def history(self, wallet, since_ts, max_sigs=400, min_sigs=0, log=print):
        """Enhanced-format transactions back to since_ts. Raises TooActive for bots;
        returns [] without fetching when there are fewer than min_sigs transactions."""
        sigs = self.signatures(wallet, since_ts, max_sigs)
        if len(sigs) < min_sigs:
            return []

        with ThreadPoolExecutor(self.workers) as ex:
            return [t for t in ex.map(self.transaction, sigs) if t]
