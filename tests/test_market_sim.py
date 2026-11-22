import pytest

from memebot import config, market, sim
from tests.conftest import fixture


def test_gt_pools_only_sol_pairs():
    pools = market.parse_gt_pools(fixture("gt_new_pools.json"))
    assert [p["symbol"] for p in pools] == ["TRB", "GIB"]       # USDC-Paar fällt raus
    trb = pools[0]
    assert trb["mint"] == "59SQ2JHHM9wB9cDtffZKrdrMSp42MpB17dcxajt3pump"
    assert trb["dex"] == "pump-fun" and trb["reserve"] == pytest.approx(3041.39, rel=1e-3)
    assert trb["created_at"] > 1.7e9
    assert market.sol_usd() == pytest.approx(70.38, rel=1e-2)


def test_ds_pairs_and_pumpfun_reserve():
    pairs = [market.parse_ds_pair(p) for p in fixture("ds_pairs.json")["pairs"]]
    gib, trb = pairs
    assert gib["liq"] == pytest.approx(17002.29) and gib["reserve"] == pytest.approx(8501.1, rel=1e-3)
    assert gib["buys_m5"] == 261 and gib["ch_m5"] == pytest.approx(79.32)
    assert trb["liq"] == 0 and trb["dex"] == "pumpfun"
    # Pump.fun-Kurve: aus dem Preis in SOL geschätzte SOL-Reserve, Größenordnung wie bei GeckoTerminal (~2–3 Tsd. $)
    assert 1000 < trb["reserve"] < 4000


def test_rugcheck_ignores_pool_accounts():
    r = market.parse_rugcheck(fixture("rugcheck_report.json"))
    assert r["top10_pct"] == 0          # 100 % liegen in der Pump.fun-Kurve, nicht bei einem Holder
    assert not r["mint_auth"] and not r["freeze_auth"] and not r["rugged"]
    assert r["holders"] == 5 and r["creator_tokens"] == 1 and r["lp_locked_pct"] == 100


def test_gt_trades_skip_anomalous():
    data = fixture("gt_trades.json")
    import memebot.market as m
    orig = m.get_json
    m.get_json = lambda *a, **k: data
    try:
        t = m.pool_trades("x")
    finally:
        m.get_json = orig
    assert [x["kind"] for x in t] == ["sell", "buy"]
    assert t[1]["wallet"] == "BdBroS2wKuzDWTZs366UggRmcsm4VqsuaTy9bvVemzA5" and t[1]["price"] > 0


def test_roundtrip_costs_money():
    q, eff, fee, imp = sim.buy(100, 0.001, 20000)
    usd, _, _ = sim.sell(q, 0.001, 20000)
    loss = 100 - usd
    # Gebühren 2x1 % + 2x Sandwich-Zuschlag + Netzwerk + Kursbewegung -> deutlich mehr als 3 $
    assert 3 < loss < 8
    assert eff > 0.001 and imp > 0


def test_small_pool_is_expensive_and_rug_is_worthless():
    q1, *_ = sim.buy(100, 1.0, 100000)
    q2, *_ = sim.buy(100, 1.0, 2000)
    assert q2 < q1 * 0.97
    usd, _, _ = sim.sell(q1, 1.0, 0)
    assert usd == 0
    usd_small, _, _ = sim.sell(q1, 1.0, 50)          # Liquidität fast weg
    assert usd_small < 50


def test_max_usd_for_impact_matches_impact():
    usd = sim.max_usd_for_impact(2.5, 10000)
    assert sim.impact_pct(usd, 10000) == pytest.approx(2.5, rel=1e-6)
    assert config.FEE_PCT == 1.0


def test_rugcheck_uses_our_pool_and_ignores_low_liquidity_flag():
    r = fixture("rugcheck_report.json")
    r["markets"] = [
        {"pubkey": "DUST", "marketType": "raydium", "lp": {"lpLockedPct": 0, "lpLockedUSD": 1, "quoteUSD": 0.5}},
        {"pubkey": "OURS", "marketType": "pump_fun_amm", "lp": {"lpLockedPct": 100, "lpLockedUSD": 9000, "quoteUSD": 4000}},
        {"pubkey": "CURVE", "marketType": "meteora_dbc", "lp": {"lpLockedPct": 0, "lpLockedUSD": 5000}},
    ]
    r["risks"] = [{"name": "Low Liquidity", "level": "danger"}, {"name": "Single holder ownership", "level": "danger"}]
    p = market.parse_rugcheck(r, ("OURS",))
    assert p["lp_locked_pct"] == 100
    assert [x["name"] for x in p["risks"]] == ["Single holder ownership"]
    p2 = market.parse_rugcheck(r, ("UNKNOWN",))     # eigener Pool fehlt: Staub-Pools und Kurven zählen nicht
    assert p2["lp_locked_pct"] == 100
