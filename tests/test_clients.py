from wt import clients, db
from wt.config import WSOL

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _pool(addr, vol, base, quote):
    return {"attributes": {"address": addr, "volume_usd": {"h24": str(vol)}},
            "relationships": {"base_token": {"data": {"id": f"solana_{base}"}},
                              "quote_token": {"data": {"id": f"solana_{quote}"}}}}

SIG = "5" * 88


class R:
    def __init__(self, j, code=200):
        self._j, self.status_code = j, code

    def json(self):
        return self._j


def test_helius_history_pages_and_resumes_after_error_object(monkeypatch):
    pages = [
        [{"signature": "a", "timestamp": 300}, {"signature": "b", "timestamp": 200}],
        {"error": f"Failed to find events within the search period. To continue search, query the API again with the `before-signature` parameter set to {SIG}."},
        [{"signature": "c", "timestamp": 150}, {"signature": "d", "timestamp": 50}],
    ]
    seen = []

    def fake_get(session, url, limiter=None, tries=8, **kw):
        seen.append(kw["params"].get("before-signature"))
        return R(pages.pop(0))

    monkeypatch.setattr(clients, "_get", fake_get)
    h = clients.Helius("k")
    out = h.history("W", since_ts=100, max_pages=10, log=lambda *_: None)
    assert [t["signature"] for t in out] == ["a", "b", "c"]
    assert seen == [None, "b", SIG]


def test_gecko_candles_cached(monkeypatch):
    calls = []

    def fake_get(session, url, limiter=None, tries=8, **kw):
        calls.append(url)
        if url.endswith("/pools"):
            return R({"data": [_pool("POOL", 10, "MINT", WSOL)]})
        return R({"data": {"attributes": {"ohlcv_list": [[120, 1, 2, 0.5, 1.5, 9], [60, 1, 1, 1, 1, 1]]}}})

    monkeypatch.setattr(clients, "_get", fake_get)
    con = db.connect(":memory:")
    g = clients.Gecko(con)
    a = g.candles("MINT", 60, 150)
    b = g.candles("MINT", 60, 150)
    assert a == b and [c[0] for c in a] == [60, 120] and len(calls) == 2


def test_jupiter_quote_shapes(monkeypatch):
    monkeypatch.setattr(clients, "_get", lambda *a, **k: R({"outAmount": "12345", "priceImpactPct": "0.02"}))
    assert clients.Jupiter("key").quote("a", "b", 1, 100) == {"out": 12345, "impact": 0.02}
    monkeypatch.setattr(clients, "_get", lambda *a, **k: R({"error": "no route"}, 400))
    assert clients.Jupiter().quote("a", "b", 1, 100) is None


def test_gecko_uses_busiest_sol_pool_and_sol_prices(monkeypatch):
    params = []

    def fake_get(session, url, limiter=None, tries=8, **kw):
        params.append(kw.get("params"))
        if url.endswith("/pools"):
            return R({"data": [_pool("USDC_POOL", 500, "MINT", USDC),
                               _pool("SOL_SMALL", 5, "MINT", WSOL),
                               _pool("SOL_BIG", 50, WSOL, "MINT")]})
        return R({"data": {"attributes": {"ohlcv_list": []}}})

    monkeypatch.setattr(clients, "_get", fake_get)
    g = clients.Gecko(db.connect(":memory:"))
    assert g.top_pool("MINT") == "SOL_BIG"
    g.candles("MINT", 60, 150)
    assert params[-1]["currency"] == "token" and params[-1]["token"] == "MINT"


def test_gecko_no_sol_pool_means_no_price(monkeypatch):
    monkeypatch.setattr(clients, "_get", lambda *a, **k: R({"data": [_pool("U", 9, "MINT", USDC)]}))
    g = clients.Gecko(db.connect(":memory:"))
    assert g.top_pool("MINT") is None and g.candles("MINT", 0, 60) == []
