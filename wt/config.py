"""Settings: defaults, overridden by settings.toml, secrets from .env / environment."""
import copy
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "paths": {
        "db": "data/whales.db",
        "wallets": "wallets.txt",
    },
    "collect": {
        "days": 30,              # how much history to pull per wallet
        "max_pages": 30,         # 100 tx per page; caps credits spent on hyperactive bots
        "min_sol": 0.01,         # ignore swaps smaller than this
    },
    "scoring": {
        "min_trips": 10,         # closed round-trips needed before we trust a wallet
        "min_profit_factor": 1.5,
        "min_win_rate": 0.35,
        "max_top_trip_share": 0.5,   # one lucky trade can't be >50% of total profit
        "min_median_hold_min": 3.0,  # faster than this = bot/sniper you can't copy
        "min_avg_size_sol": 0.2,
        "max_avg_size_sol": 500.0,
    },
    "backtest": {
        "train_frac": 0.6,       # score on first 60% of time, test copying on last 40%
        "top_n": 15,             # how many wallets to follow in the test period
        "start_sol": 10.0,
        "size_sol": 0.5,         # per trade
        "max_open": 5,
        "delay_sec": 20,         # detection + execution latency after whale's tx
        "slippage_bps": 150,     # per side, on top of the candle price
        "fee_sol": 0.0006,       # base + priority fee per tx
        "fail_rate": 0.05,       # share of buys that fail on-chain (fee lost)
        "take_profit": 0.5,      # +50%
        "stop_loss": 0.25,       # -25%
        "trailing_stop": 0.0,    # 0 = off; e.g. 0.2 = exit 20% below peak
        "max_hold_min": 240,
        "follow_whale_exit": True,
        "seed": 7,
    },
    "live": {
        "start_sol": 10.0,
        "size_sol": 0.5,
        "max_open": 5,
        "reserve_sol": 0.1,          # never spend the last bit (fees)
        "min_whale_buy_sol": 0.5,    # ignore small whale buys
        "confluence_wallets": 1,     # require N followed wallets buying same token...
        "confluence_window_min": 60, # ...within this window
        "exec_latency_sec": 2.0,     # simulated time to build/land our tx
        "slippage_bps": 300,         # max slippage passed to Jupiter
        "fill_haircut_bps": 50,      # realized fill worse than quote by this much
        "fee_sol": 0.0006,
        "fail_rate": 0.05,
        "take_profit": 0.5,
        "stop_loss": 0.25,
        "trailing_stop": 0.0,
        "max_hold_min": 240,
        "follow_whale_exit": True,
        "monitor_sec": 15,
        "write_off_min": 15,         # no sell route this long = treat as rugged (0)
        "follow_file": "follow.txt", # written by `backtest`, editable by hand
    },
}

STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
WSOL = "So11111111111111111111111111111111111111112"


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(path="settings.toml"):
    load_env()
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path)
    if p.exists():
        with p.open("rb") as f:
            _merge(cfg, tomllib.load(f))
    cfg["keys"] = {
        "helius": os.environ.get("HELIUS_API_KEY", ""),
        "jupiter": os.environ.get("JUP_API_KEY", ""),
    }
    return cfg


def read_wallets(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        w = line.split("#", 1)[0].strip()
        if w and w not in out:
            out.append(w)
    return out
