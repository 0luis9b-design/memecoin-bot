"""v1.2: neue Ausstiege, Marktlage, Copy-Rückmeldung, neue Merkmale, Replay-Backtest."""
import json
import time

import pytest

from memebot import config, params, rules
from memebot.agents import analysis, wallets
from memebot.agents.regime import Regime
from memebot.backtest import Backtester, Data, run_variant
from memebot.engine import Engine
from tests.test_engine import GOOD_RUG, FakeMarket, main_acc, pair

P = params.defaults("scanner")
NOW = 1_800_000_000.0


def st(**kw):
    base = {"market_entry": 1.0, "peak_price": 1.0, "opened_at": NOW, "tp1_done": 0, "entry_liq": 10000, "stop_pct": None, "flow_since": 0}
    return dict(base, **kw)


def test_time_stop():
    p = dict(P, time_stop_min=10, time_stop_gain_pct=5)
    assert rules.exit_decision(p, st(), 1.02, 10000, 50, 40, NOW + 5 * 60)[0] is None
    a, _, why, _ = rules.exit_decision(p, st(), 1.02, 10000, 50, 40, NOW + 11 * 60)
    assert a == "exit" and "Zeit-Stop" in why
    assert rules.exit_decision(p, st(peak_price=1.2), 1.02, 10000, 50, 40, NOW + 11 * 60)[0] is None   # war schon +20 %


def test_flow_exit_needs_30_seconds():
    p = dict(P, flow_exit=1, flow_ratio=1.3)
    a, _, _, since = rules.exit_decision(p, st(), 1.0, 10000, 20, 60, NOW + 60)
    assert a is None and since == NOW + 60
    a, _, _, since = rules.exit_decision(p, st(flow_since=since), 1.0, 10000, 20, 60, NOW + 75)
    assert a is None
    a, _, why, _ = rules.exit_decision(p, st(flow_since=since), 1.0, 10000, 20, 60, NOW + 95)
    assert a == "exit" and "Kaufdruck gekippt" in why
    assert rules.exit_decision(p, st(flow_since=since), 1.0, 10000, 80, 60, NOW + 95)[3] == 0     # wieder normal -> zurückgesetzt


def test_vol_stop_and_order():
    assert rules.stop_for(P, 40) == 15                       # aus -> fester Stop
    p = dict(P, vol_stop=1, vol_mult=0.5)
    assert rules.stop_for(p, 40) == 20 and rules.stop_for(p, 4) == 6 and rules.stop_for(p, 200) == 35
    a, _, why, _ = rules.exit_decision(P, st(stop_pct=20), 0.85, 10000, 1, 1, NOW + 60)
    assert a is None                                          # −15 % reicht bei 20 % Stop nicht
    a, _, why, _ = rules.exit_decision(P, st(stop_pct=20), 0.79, 10000, 1, 1, NOW + 60)
    assert a == "exit" and "Stop-Loss" in why


def test_regime_mult_and_pause():
    p = dict(P, regime_cold_mult=0.5, regime_pause_heat=15)
    assert rules.regime_mult(p, 10) is None and rules.regime_mult(p, 20) == 0.5 and rules.regime_mult(p, 50) == 1.0
    assert rules.regime_mult(P, 10) == 1.0 and rules.regime_mult(P, None) == 1.0


def test_regime_update(store):
    now = time.time()
    for i in range(300):
        store.x("INSERT INTO launches(mint,curve,creator,ts) VALUES(?,?,?,?)", (f"L{i}", "c", "x", now - 60))
    for i in range(30):
        store.x("INSERT INTO tokens(mint,pool,status,hot,updated,metrics) VALUES(?,?,?,?,?,?)",
                (f"T{i}", f"P{i}", "watch", 1, now, json.dumps({"ch_h1": 50})))
    r = Regime(store)
    heat = r.update()
    assert heat > 60 and r.label(heat) == "heiß"
    assert store.one("SELECT heat FROM regime")["heat"] == heat
    assert Regime(store).heat == heat                          # nach Neustart wieder da


def test_cold_market_pauses_variant(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    e = Engine(store)
    e.regime.heat = 5
    fm.add(pair("POOLK", "MINTK", "COLD", 0.001))
    e.cycle()
    accs = {a["name"]: a for a in e.trader.accounts("scanner")}
    assert e.trader.open_positions(accs["Scanner"]["id"])                 # Hauptkonto: Marktlage aus
    assert not e.trader.open_positions(accs["Scanner · Marktlage"]["id"])  # Testkonto pausiert
    assert e.s.q("SELECT * FROM decisions WHERE reason LIKE '%Markt zu kalt%'")


def test_new_variants_added_to_old_database(store, monkeypatch):
    FakeMarket(monkeypatch)
    e = Engine(store)
    e.s.x("DELETE FROM accounts WHERE name='Scanner · Zeit-Stop'")
    Engine(store)
    acc = store.one("SELECT * FROM accounts WHERE name='Scanner · Zeit-Stop'")
    assert acc and json.loads(acc["params"])["time_stop_min"] == 10
    # alte Konten ohne neue Regeln bekommen Standardwerte
    store.x("UPDATE accounts SET params=? WHERE name='Scanner'", (json.dumps({k: v for k, v in P.items() if k != "flow_exit"}),))
    assert main_acc(e)["params"]["flow_exit"] == 0


def test_copy_feedback_adjusts_and_drops_wallet():
    assert wallets.Scout.adjust(0.6, None) == (0.6, None)
    sc, _ = wallets.Scout.adjust(0.6, {"n": 4, "wins": 3, "pnl": 40, "pct": 100})   # +25 % im Schnitt
    assert sc == pytest.approx(0.9)
    sc, note = wallets.Scout.adjust(0.6, {"n": 6, "wins": 1, "pnl": -30, "pct": -120})
    assert sc == 0 and "Kopien verlieren" in note


def test_copy_results_from_positions(store, monkeypatch):
    FakeMarket(monkeypatch)
    e = Engine(store)
    acc = main_acc(e, "copy")
    for i, pnl in enumerate([-10, -10, -10, -10, -10, 5]):
        e.s.x("""INSERT INTO positions(account_id,mint,symbol,status,created,closed_at,cost,proceeds,signal) VALUES(?,?,?,?,?,?,?,?,?)""",
              (acc["id"], f"M{i}", "T", "closed", time.time(), time.time(), 50, 50 + pnl, json.dumps({"copy": {"wallet": "WX"}})))
    r = e.scout.copy_results()["WX"]
    assert r["n"] == 6 and r["wins"] == 1 and r["pnl"] == -45


def test_new_features_have_no_weight_by_default():
    d = pair("P", "M", "S", 0.001)
    d["age_min"] = 20
    d["boosts"] = 3
    comps = analysis.components(d, [], dict(GOOD_RUG, holder_growth_h=150))
    assert comps["kein_boost"][0] == 0 and comps["holder_wachstum"][0] == pytest.approx(0.5)
    s1, det = analysis.total(comps)
    d["boosts"] = 0
    s2, _ = analysis.total(analysis.components(d, [], dict(GOOD_RUG, holder_growth_h=150)))
    assert s1 == s2 and det["kein_boost"]["gewicht"] == 0


# ---------------------------------------------------------------- Replay-Backtest
def _record(store, mint, prices, start, liq=40000, rug=None, step=20):
    store.x("INSERT INTO tokens(mint,pool,symbol,dex,created_at,first_seen,status,rug) VALUES(?,?,?,?,?,?,?,?)",
            (mint, "P" + mint, mint[:6], "raydium", start - 1200, start, "watch", json.dumps(rug or GOOD_RUG)))
    rows = []
    for i, px in enumerate(prices):
        prev = prices[max(0, i - 15)]
        rows.append((mint, start + i * step, px, px * 1e9, liq, 15000, 150, 60, (px / prev - 1) * 100, 40, 40000, liq / 2, 0, 2))
    store.many("INSERT INTO snapshots(mint,ts,price,mcap,liq,vol_m5,buys_m5,sells_m5,ch_m5,ch_h1,vol_h1,reserve,boosts,socials) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def test_replay_runs_rules_with_costs(store):
    start = time.time() - 6 * 3600
    up = [0.001 * (1.004 ** i) for i in range(400)]                       # stetig steigend
    _record(store, "PUMPMINT", up, start)
    data = Data(store, 8)
    assert "PUMPMINT" in data.series and data.candidates
    res = run_variant(data, P, data.since, data.until)
    assert res["trades"] >= 1 and res["rendite"] > 0
    crash = [0.001 * (1.004 ** i) for i in range(30)] + [0.0005] * 50
    _record(store, "DUMPMINT", crash, start + 3 * 3600)
    data = Data(store, 8)
    res2 = run_variant(data, dict(P, max_positions=1), start + 3 * 3600 - 60, data.until)
    assert res2["trades"] >= 1 and res2["rendite"] < 0                    # Stop-Loss kostet Geld


def test_backtester_picks_robust_variant(store, monkeypatch):
    fm = FakeMarket(monkeypatch)   # noqa: F841 – Konten anlegen ohne echte Abrufe
    e = Engine(store)
    start = time.time() - 30 * 3600
    for k in range(12):
        prices = [0.001 * (1.003 ** i) for i in range(300)]
        _record(store, f"MINT{k:02d}", prices, start + k * 2.4 * 3600, liq=6000)   # Liquidität unter der Mindestgrenze 8000
    monkeypatch.setattr(config, "BACKTEST_MIN_TRADES", 3)
    summary, ranked = e.backtester.run("test", hours=32, n_variants=40)
    assert summary["regelsaetze"] >= 40 and ranked[0]["name"].startswith("Hauptkonto")
    assert ranked[0]["train"]["trades"] == 0                                # Hauptkonto lässt diese Coins aus
    robust = [r for r in ranked if r["robust"]]
    if robust:   # eine Variante mit lockerer Liquiditätsregel findet die Gewinne
        assert robust[0]["params"]["min_liq"] <= 6000
        assert summary["testkonto"] and store.one("SELECT * FROM accounts WHERE name LIKE 'Scanner · Replay%'")
    assert store.one("SELECT status FROM backtests ORDER BY id DESC")["status"] == "fertig"


def test_backtest_api(store):
    import app as appmod
    from tests.test_learning_app import auth
    c = appmod.app.test_client()
    r = c.get("/api/backtest", headers=auth())
    assert r.status_code == 200 and "results" in r.json
    assert c.get("/api/regime?hours=24", headers=auth()).status_code == 200
    r = c.get("/api/shadow", headers=auth())
    assert len(r.json["uhrzeit"]) == 24
    assert isinstance(Backtester, type)
