"""Thin HTTP clients: Helius (wallet history + tx parsing), GeckoTerminal (candles),
Jupiter (real executable quotes for paper fills)."""
import re
import threading
import time

import requests

from .config import WSOL

SIG_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{80,90}")


class RateLimiter:
    def __init__(self, per_min):
        self.gap = 60.0 / per_min
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:  # reserve the next slot, then sleep outside the lock
            slot = max(self.last + self.gap, time.time())
            self.last = slot
        d = slot - time.time()
        if d > 0:
            time.sleep(d)


def _get(session, url, limiter=None, tries=8, **kw):
    for i in range(tries):
        if limiter:
            limiter.wait()
        try:
            r = session.get(url, timeout=30, **kw)
        except requests.RequestException:
            time.sleep(2 ** i)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(2 ** i + 1, 30))  # 429s come in bursts; wait them out
            continue
        return r
    raise RuntimeError(f"GET failed after {tries} tries: {url}")


class Helius:
    def __init__(self, key, base="https://api-mainnet.helius-rpc.com"):
        if not key:
            raise SystemExit("HELIUS_API_KEY missing. Put it in .env (free key at dashboard.helius.dev)")
        self.key = key
        self.base = base
        self.s = requests.Session()
        self.lim = RateLimiter(240)  # stay well under free-plan limits

    def history(self, wallet, since_ts, max_pages=30, log=print):
        """Newest-first transaction history back to since_ts (enhanced format)."""
        out, cursor, empty_retries = [], None, 0
        url = f"{self.base}/v0/addresses/{wallet}/transactions"
        for _ in range(max_pages):
            params = {"api-key": self.key, "limit": 100, "sort-order": "desc",
                      "token-accounts": "balanceChanged"}
            if cursor:
                params["before-signature"] = cursor
            r = _get(self.s, url, self.lim, params=params)
            data = r.json()
            if isinstance(data, dict):  # API returns an error object with a resume cursor
                m = SIG_RE.search(str(data))
                if m and empty_retries < 5:
                    cursor, empty_retries = m.group(0), empty_retries + 1
                    continue
                log(f"  helius: {str(data)[:160]}")
                break
            if not data:
                break
            out.extend(data)
            cursor = data[-1]["signature"]
            if data[-1].get("timestamp", 0) < since_ts:
                break
        return [t for t in out if t.get("timestamp", 0) >= since_ts]

    def window(self, address, gte_ts, lte_ts, max_pages=10):
        """All transactions of `address` between two unix times, newest first.
        Returns (txs, complete): complete is False when max_pages ran out first."""
        out, cursor = [], None
        url = f"{self.base}/v0/addresses/{address}/transactions"
        for _ in range(max_pages):
            params = {"api-key": self.key, "limit": 100, "sort-order": "desc",
                      "gte-time": int(gte_ts), "lte-time": int(lte_ts)}
            if cursor:
                params["before-signature"] = cursor
            data = _get(self.s, url, self.lim, params=params).json()
            if isinstance(data, dict):
                m = SIG_RE.search(str(data))
                if m and m.group(0) != cursor:  # gap in the history: resume after it
                    cursor = m.group(0)
                    continue
                break
            if not data:
                return out, True
            if any(t.get("timestamp", 0) > lte_ts + 120 for t in data):
                raise RuntimeError("Helius ignored the time window (gte-time/lte-time); "
                                   "can't search a time range on this plan/API version")
            out.extend(t for t in data if t.get("timestamp", 0) >= gte_ts)
            cursor = data[-1]["signature"]
            if data[-1].get("timestamp", 0) < gte_ts or len(data) < 100:
                return out, True
        return out, False

    def parse(self, sigs):
        r = self.s.post(f"{self.base}/v0/transactions", params={"api-key": self.key},
                        json={"transactions": list(sigs)}, timeout=30)
        if r.status_code != 200:
            return []
        return r.json() or []

    def ws_url(self):
        return f"wss://mainnet.helius-rpc.com/?api-key={self.key}"


def _other_side(pool, mint):
    """Mint on the other side of a GeckoTerminal pool from `mint` (ids look like 'solana_<mint>')."""
    rel = pool.get("relationships") or {}
    ids = [((rel.get(k) or {}).get("data") or {}).get("id", "") for k in ("base_token", "quote_token")]
    ids = [i.split("_", 1)[-1] for i in ids]
    if mint not in ids:
        return None
    return ids[1] if ids[0] == mint else ids[0]


class Gecko:
    """GeckoTerminal public API: 30 calls/min, minute candles for the last ~6 months.
    Candles are SOL per token, from the token's busiest SOL-paired pool."""
    BASE = "https://api.geckoterminal.com/api/v2"
    BLOCK = 60_000  # seconds in 1000 one-minute candles

    def __init__(self, con):
        self.con = con
        self.s = requests.Session()
        self.s.headers["accept"] = "application/json"
        self.lim = RateLimiter(20)

    def top_pool(self, mint):
        row = self.con.execute("SELECT pool FROM pools WHERE mint=?", (mint,)).fetchone()
        if row:
            return row[0] or None
        r = _get(self.s, f"{self.BASE}/networks/solana/tokens/{mint}/pools", self.lim,
                 params={"page": 1})
        pool = None
        if r.status_code == 200:
            # Only SOL-paired pools, so candles (currency=token) are priced in SOL like the
            # rest of the PnL. Most-traded one is the best reference, even if it has since rugged.
            pools = [p for p in (r.json().get("data") or []) if _other_side(p, mint) == WSOL]
            if pools:
                best = max(pools, key=lambda p: float(
                    (p["attributes"].get("volume_usd") or {}).get("h24") or 0))
                pool = best["attributes"]["address"]
        self.con.execute("INSERT OR REPLACE INTO pools VALUES(?,?,?)", (mint, pool, int(time.time())))
        self.con.commit()
        return pool

    def candles(self, mint, start_ts, end_ts):
        """Minute candles [ts,o,h,l,c,v] covering start..end (cached)."""
        pool = self.top_pool(mint)
        if not pool:
            return []
        # Fetch in fixed 1000-minute blocks (the API's page size), so signals on the same
        # token that are close in time share one request instead of one each.
        for k in range(int(start_ts // self.BLOCK), int(end_ts // self.BLOCK) + 1):
            before = (k + 1) * self.BLOCK
            done = self.con.execute(
                "SELECT 1 FROM candle_fetches WHERE pool=? AND mint=? AND before_ts=?",
                (pool, mint, before)).fetchone()
            if done:
                continue
            r = _get(self.s, f"{self.BASE}/networks/solana/pools/{pool}/ohlcv/minute", self.lim,
                     params={"aggregate": 1, "before_timestamp": before, "limit": 1000,
                             "currency": "token", "token": mint})
            rows = []
            if r.status_code == 200:
                lst = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                rows = [(pool, mint, int(c[0]), *map(float, c[1:6])) for c in lst]
            self.con.executemany("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?)", rows)
            if before <= time.time():  # a block still in progress must be fetched again later
                self.con.execute("INSERT OR IGNORE INTO candle_fetches VALUES(?,?,?)", (pool, mint, before))
            self.con.commit()
        rows = self.con.execute(
            "SELECT ts,o,h,l,c,v FROM candles WHERE pool=? AND mint=? AND ts>=? AND ts<=? ORDER BY ts",
            (pool, mint, int(start_ts // 60 * 60), int(end_ts))).fetchall()
        return [tuple(r) for r in rows]


def hour_candles(g, pool, mint, start_ts, end_ts):
    """Hourly candles [ts,o,h,l,c,v] (SOL per token) for a pool, cached in hour_candles."""
    have = g.con.execute("SELECT MIN(ts), MAX(ts) FROM hour_candles WHERE pool=? AND mint=?",
                         (pool, mint)).fetchone()
    if have[0] is None or have[0] > start_ts + 3600 or have[1] < end_ts - 7200:
        before = int(end_ts)
        while before > start_ts:
            r = _get(g.s, f"{g.BASE}/networks/solana/pools/{pool}/ohlcv/hour", g.lim,
                     params={"aggregate": 1, "before_timestamp": before, "limit": 1000,
                             "currency": "token", "token": mint})
            lst = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                   if r.status_code == 200 else [])
            g.con.executemany("INSERT OR IGNORE INTO hour_candles VALUES(?,?,?,?,?,?,?,?)",
                              [(pool, mint, int(c[0]), *map(float, c[1:6])) for c in lst])
            g.con.commit()
            if len(lst) < 1000:
                break  # pool didn't exist earlier
            before = min(int(c[0]) for c in lst)
    rows = g.con.execute("SELECT ts,o,h,l,c,v FROM hour_candles WHERE pool=? AND mint=? "
                         "AND ts>=? AND ts<=? ORDER BY ts", (pool, mint, int(start_ts), int(end_ts)))
    return [tuple(r) for r in rows]


class Jupiter:
    """Real executable quotes. With JUP_API_KEY uses api.jup.ag (free key at portal.jup.ag);
    without, falls back to the keyless lite-api (lower limits, may be retired)."""

    def __init__(self, key=""):
        self.base = "https://api.jup.ag/swap/v1" if key else "https://lite-api.jup.ag/swap/v1"
        self.s = requests.Session()
        if key:
            self.s.headers["x-api-key"] = key
        self.lim = RateLimiter(50)

    def quote(self, in_mint, out_mint, amount_raw, slippage_bps):
        try:
            r = _get(self.s, f"{self.base}/quote", self.lim, tries=3, params={
                "inputMint": in_mint, "outputMint": out_mint, "amount": int(amount_raw),
                "slippageBps": int(slippage_bps), "restrictIntermediateTokens": "true"})
        except RuntimeError:
            return None
        if r.status_code != 200:
            return None
        j = r.json()
        if "outAmount" not in j:
            return None
        return {"out": int(j["outAmount"]), "impact": float(j.get("priceImpactPct") or 0)}
