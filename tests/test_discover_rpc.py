from wt.config import WSOL
from wt.discover import rank_wallets
from wt.parse import parse_tx
from wt.rpc import SolanaRPC, TooActive, to_enhanced

W = "WalletAAAA"


def _raw(sol_delta_lamports, pre_tok, post_tok, err=None):
    return {"blockTime": 1234, "transaction": {"message": {"accountKeys": [W, "Pool"]}},
            "meta": {"err": err, "preBalances": [5_000_000_000, 0], "postBalances": [5_000_000_000 + sol_delta_lamports, 0],
                     "loadedAddresses": {"writable": ["Extra"], "readonly": []},
                     "preTokenBalances": ([{"accountIndex": 2, "mint": "MINT", "owner": W,
                                            "uiTokenAmount": {"amount": str(pre_tok), "decimals": 6}}] if pre_tok else []),
                     "postTokenBalances": [{"accountIndex": 2, "mint": "MINT", "owner": W,
                                            "uiTokenAmount": {"amount": str(post_tok), "decimals": 6}}]}}


def test_raw_rpc_buy_and_sell_parse_like_helius():
    buy = parse_tx(to_enhanced("s1", _raw(-2_000_000_000, 0, 7_000_000)), W)
    assert (buy["side"], buy["sol_amt"], buy["token_amt"], buy["ts"]) == ("buy", 2.0, 7.0, 1234)
    sell = parse_tx(to_enhanced("s2", _raw(+3_000_000_000, 7_000_000, 0)), W)
    assert (sell["side"], sell["sol_amt"], sell["token_amt"]) == ("sell", 3.0, 7.0)
    assert parse_tx(to_enhanced("s3", _raw(-1, 0, 5, err={"x": 1})), W) is None  # failed tx


def test_signatures_stop_at_window_and_flag_bots():
    class R(SolanaRPC):
        def __init__(self, pages):
            self.pages = pages

        def call(self, method, params, tries=12):
            return self.pages.pop(0)

    page = [{"signature": f"s{i}", "blockTime": 1000 - i, "err": None if i % 2 else {"e": 1}} for i in range(10)]
    assert R([page]).signatures(W, since_ts=995, max_sigs=100) == ["s1", "s3", "s5"]
    busy = [{"signature": f"s{i}", "blockTime": 5000, "err": None} for i in range(1000)]
    try:
        R([busy, busy]).signatures(W, since_ts=0, max_sigs=500)
        assert False, "expected TooActive"
    except TooActive:
        pass


def _t(wallet, kind, sol, anomalous=False):
    base = {"tx_from_address": wallet, "kind": kind, "is_anomalous": anomalous}
    if kind == "buy":
        return {**base, "from_token_address": WSOL, "to_token_address": "T", "from_token_amount": str(sol), "to_token_amount": "1"}
    return {**base, "from_token_address": "T", "to_token_address": WSOL, "from_token_amount": "1", "to_token_amount": str(sol)}


def test_rank_wallets_prefers_breadth_and_drops_bots_and_sell_only():
    trades = {
        "M1": [_t("multi", "buy", 1), _t("winner", "buy", 2), _t("winner", "sell", 5), _t("seller", "sell", 9)]
              + [_t("bot", "buy", 1)] * 50,
        "M2": [_t("multi", "buy", 1), _t("fake", "buy", 3, anomalous=True)],
    }
    out = rank_wallets(trades, max_trades=40)
    assert [c["wallet"] for c in out] == ["winner", "multi"]  # closed a profit first, then by size
    assert out[1]["tokens"] == 2 and out[0]["closed_pnl"] == 3.0


def test_human_paced_drops_bots_by_onchain_activity():
    import time
    from wt.discover import human_paced
    now = time.time()

    class R:
        def call(self, method, params):
            w = params[0]
            if w == "bot":   # 1000 txs in the last 2 hours
                return [{"blockTime": now - 7200, "signature": f"{w}:x"}] * 1000
            if w in ("human", "mover"):  # 1000 txs over 40 days = 25/day
                return [{"blockTime": now - 40 * 86400, "signature": f"{w}:x"}] * 1000
            return [{"blockTime": now - 86400, "signature": f"{w}:x"}] * 90   # 90 txs in total, ever

        def transaction(self, sig):
            w = sig.split(":")[0]
            if w == "mover":  # human-paced but never swaps
                return to_enhanced(sig, _raw(0, 0, 0))
            raw = _raw(-2_000_000_000, 0, 7_000_000)
            raw["transaction"]["message"]["accountKeys"][0] = w
            for b in raw["meta"]["postTokenBalances"]:
                b["owner"] = w
            return to_enhanced(sig, raw)

    out = human_paced([{"wallet": w} for w in ("bot", "human", "mover", "new")], 10, 60, R(), log=lambda *_: None)
    assert [c["wallet"] for c in out] == ["human", "new"] and round(out[0]["tx_per_day"]) == 25
    assert out[0]["swaps_in_sample"] == 8
