"""Launch-Feed, Schatten-Auswertung, Serien-Launcher, Gewichts-Lernen, schnelle Spur."""
import json
import time

import pytest

from memebot import config, params
from memebot.agents import analysis, launches
from memebot.agents.shadow import Shadow, classify
from memebot.engine import Engine
from tests.test_engine import GOOD_RUG, FakeMarket, main_acc, pair

CREATE = ('{"signature":"3mZL","mint":"GwAMdBuTcCzLJuSzVcdyDGU7hPht1D63sHboNED9pump","traderPublicKey":"39WgDrVwJ8irBQuvhJ296ZSUs6Wsw1H9rtxb88ZbuC59",'
          '"txType":"create","initialBuy":20104668.026156,"solAmount":0.572839505,"bondingCurveKey":"CbXjSbNrsJhtR6sJXKBZ98asZu1oxUt6vQvQDkeCXFxu",'
          '"vTokensInBondingCurve":1052895331.97,"vSolInBondingCurve":30.57,"marketCapSol":29.03,"name":"MUPHY","symbol":"MUPHY",'
          '"uri":"https://ipfs.io/ipfs/x","is_mayhem_mode":true,"pool":"pump"}')


@pytest.fixture
def eng(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    return Engine(store), fm


def test_parse_pumpportal_messages():
    k, d = launches.parse_message(CREATE)
    assert k == "create" and d["mint"].endswith("pump") and d["curve"].startswith("CbXj") and d["creator"].startswith("39Wg")
    assert d["initial_sol"] == pytest.approx(0.5728, rel=1e-3) and d["symbol"] == "MUPHY"
    assert launches.parse_message('{"message":"Successfully subscribed to token creation events."}') == (None, None)
    assert launches.parse_message('{"txType":"migrate","mint":"ABC","pool":"pump-amm"}') == ("migrate", "ABC")
    assert launches.parse_message("kein json") == (None, None)


def test_launch_is_checked_then_watched(eng, monkeypatch):
    e, fm = eng
    e.launches.handle(CREATE)
    row = e.s.one("SELECT * FROM launches")
    assert row["status"] == "neu" and row["creator"].startswith("39Wg")
    monkeypatch.setattr(config, "LAUNCH_CHECK_MIN_SEC", 0)
    d = pair("CbXjSbNrsJhtR6sJXKBZ98asZu1oxUt6vQvQDkeCXFxu", "GwAMdBuTcCzLJuSzVcdyDGU7hPht1D63sHboNED9pump", "MUPHY", 0.00002,
             vol_m5=5000, age_min=3)
    fm.p[d["pool"]] = d
    assert e.launches.check(e.scanner) == 1
    assert e.s.one("SELECT status FROM tokens WHERE mint=?", (d["mint"],))["status"] == "watch"
    assert e.s.one("SELECT status FROM launches")["status"] == "aufgenommen"


def test_quiet_launch_is_dropped(eng, monkeypatch):
    e, fm = eng
    e.launches.handle(CREATE)
    monkeypatch.setattr(config, "LAUNCH_CHECK_MIN_SEC", 0)
    d = pair("CbXjSbNrsJhtR6sJXKBZ98asZu1oxUt6vQvQDkeCXFxu", "GwAMdBuTcCzLJuSzVcdyDGU7hPht1D63sHboNED9pump", "MUPHY", 0.000001,
             vol_m5=100, buys=2, liq=1000, age_min=3)
    d["mcap"] = 4000
    fm.p[d["pool"]] = d
    for _ in range(3):
        e.s.x("UPDATE launches SET checked_at=0")
        e.launches.check(e.scanner)
    assert e.s.one("SELECT status FROM launches")["status"] == "verworfen"
    assert not e.s.one("SELECT * FROM tokens")


def test_migration_moves_open_position(eng):
    e, fm = eng
    fm.add(pair("CURVE1", "MINTM", "MIG", 0.001))
    e.cycle(); e.cycle()
    assert e.trader.open_positions(main_acc(e)["id"])[0]["pool"] == "CURVE1"
    new = pair("AMM1", "MINTM", "MIG", 0.001, liq=80000)
    new["dex"] = "pumpswap"
    del fm.p["CURVE1"]
    fm.p["AMM1"] = new
    e.launches.migrations.append("MINTM")
    assert e.launches.handle_migrations(e.scanner) == 1
    assert e.trader.open_positions(main_acc(e)["id"])[0]["pool"] == "AMM1"
    e.cycle()
    assert e.trader.open_positions(main_acc(e)["id"])[0]["status"] == "open"   # kein Rug-Alarm durch den Umzug


def test_lost_pool_is_found_again(eng, monkeypatch):
    e, fm = eng
    monkeypatch.setattr(config, "MISSING_RUG_MIN", 15)
    fm.add(pair("OLD", "MINTL", "LOST", 0.001))
    e.cycle(); e.cycle()
    del fm.p["OLD"]
    e.cycle()
    e.s.x("UPDATE tokens SET missing_since=? WHERE mint='MINTL'", (time.time() - 600,))
    fm.p["NEW"] = pair("NEW", "MINTL", "LOST", 0.001)
    assert e.find_lost_pools() == 1
    assert e.trader.open_positions(main_acc(e)["id"])[0]["pool"] == "NEW"


def test_serial_launcher_blocked(eng):
    e, fm = eng
    creator = "SerialDev111"
    for i in range(8):
        e.s.x("INSERT INTO launches(mint,curve,creator,ts) VALUES(?,?,?,?)", (f"X{i}", f"C{i}", creator, time.time()))
    fm.rug["MINTS"] = dict(GOOD_RUG, creator=creator)
    fm.add(pair("POOLS", "MINTS", "SERIE", 0.001))
    e.cycle(); e.cycle()
    assert not e.trader.open_positions()
    b = e.s.q("SELECT reason FROM decisions WHERE action='BLOCK'")
    assert b and "Serien-Launcher" in b[0]["reason"]
    sh = e.s.q("SELECT rule FROM shadow WHERE mint='MINTS'")
    assert [r["rule"] for r in sh] == ["rug:serie"]


def test_fast_lane_for_open_positions(eng, monkeypatch):
    e, fm = eng
    fm.add(pair("POOLF", "MINTF", "FAST", 0.001))
    fm.add(pair("POOLQ", "MINTQ", "QUIET", 0.001, vol_m5=100, buys=5))
    e.cycle()
    monkeypatch.setattr(config, "REFRESH_OPEN_SEC", 5)
    monkeypatch.setattr(config, "REFRESH_ALL_SEC", 120)
    monkeypatch.setattr(config, "REFRESH_HOT_SEC", 20)
    e.s.x("UPDATE tokens SET updated=?", (time.time() - 6,))
    assert e.scanner.due_pools(False) == ["POOLF"]   # nur die offene Position ist nach 6 s schon wieder dran


def test_classify_outcomes():
    assert classify(1, 1.8, 1.2, 1000, 900, 1)[0] == "gewinner"
    assert classify(1, 1.8, 0.1, 1000, 900, 1)[0] == "rug"
    assert classify(1, 1.05, 0.9, 1000, 1000, 1)[0] == "verlierer"
    assert classify(1, 1.2, 1.1, 1000, 100, 1)[0] == "rug"          # Liquidität weg
    assert classify(1, 1.2, 1.1, 1000, 1000, None)[0] == "unbekannt"


def _shadow_rows(sh, rule, n, outcome_price, strategy="scanner"):
    for i in range(n):
        d = {"price": 1.0, "liq": 10000}
        sh._seen.clear()
        sh.record(strategy, f"{rule}-{i}", "T", rule, "x", d)
    sh.s.x("UPDATE shadow SET ts=?, updated=?, max_price=?, last_price=? WHERE rule=?",
           (time.time() - 3 * 3600, time.time(), outcome_price[0], outcome_price[1], rule))


def test_shadow_report_and_suggestion(store, monkeypatch):
    monkeypatch.setattr(config, "SHADOW_MIN_N", 10)
    sh = Shadow(store)
    _shadow_rows(sh, "gekauft", 20, (1.3, 0.9))        # Käufe: kaum Gewinner
    _shadow_rows(sh, "liquiditaet", 20, (2.0, 1.5))    # abgelehnt wegen Liquidität: alles Gewinner
    _shadow_rows(sh, "rug:top10", 20, (1.0, 0.05))     # abgelehnt wegen Top 10: alles Rugs
    assert sh.finalize() == 60
    rep = sh.report()["scanner"]
    rules = {r["rule"]: r for r in rep["regeln"]}
    assert rules["liquiditaet"]["gewinner_pct"] == 100 and rules["liquiditaet"]["einschaetzung"] == "eher zu streng"
    assert rules["rug:top10"]["einschaetzung"] == "schützt gut"
    assert rep["regeln"][0]["rule"] == "gekauft"
    sug = sh.suggestion("scanner")
    assert sug["param"] == "min_liq" and sug["direction"] == -1
    p, changed = params.loosen(params.defaults("scanner"), "scanner", "min_liq", -1)
    assert changed == {"min_liq": (8000, 7000)}


def test_shadow_records_rejections_and_buys(eng):
    e, fm = eng
    fm.add(pair("POOLA", "MINTA", "GOOD", 0.001))
    fm.add(pair("POOLB", "MINTB", "THIN", 0.001, liq=3000))
    e.cycle(); e.cycle()
    rules = {(r["mint"], r["rule"]) for r in e.s.q("SELECT mint, rule FROM shadow")}
    assert ("MINTA", "gekauft") in rules and ("MINTB", "liquiditaet") in rules
    fm.p["POOLB"]["price"] = 0.003
    e.cycle()
    r = e.s.one("SELECT * FROM shadow WHERE mint='MINTB'")
    assert r["max_price"] == pytest.approx(0.003)


def test_weights_change_score_and_learning_creates_variant(eng, monkeypatch):
    e, _ = eng
    d = pair("P", "M", "S", 0.001)
    d["age_min"] = 20
    comps = analysis.components(d, [], dict(GOOD_RUG))
    s_default, _ = analysis.total(comps)
    s_mom, det = analysis.total(comps, {"momentum": 80, "kaufdruck": 5, "beschleunigung": 5, "aktivitaet": 5,
                                        "liquiditaet": 5, "sicherheit": 5, "chart": 5})
    assert s_default != s_mom and abs(sum(v["gewicht"] for v in det.values()) - 100) < 0.5
    # Beispiele: Gewinner haben hohes Momentum, Verlierer niedriges
    monkeypatch.setattr(config, "WEIGHT_MIN_SAMPLES", 40)
    rows = []
    for i in range(60):
        win = i % 2 == 0
        parts = {k: 0.5 for k in analysis.WEIGHTS}
        parts["momentum"] = 0.9 if win else 0.1
        rows.append((f"W{i}", "T", "scanner", "punktzahl", "x", time.time() - 9000, 1.0, 1000, 2.0 if win else 1.0, 1.0,
                     1.5 if win else 0.05, 1000, time.time(), json.dumps(parts), 1, 0, 0, "gewinner" if win else "rug"))
    e.s.many("""INSERT INTO shadow(mint,symbol,strategy,rule,reason,ts,price,liq,max_price,min_price,last_price,last_liq,updated,parts,
                done,max_gain,final,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    rep = e.learner.weight_analysis()
    assert rep["neu"]["momentum"] > analysis.WEIGHTS["momentum"]
    assert e.learner.learn_weights() is True
    v = e.s.one("SELECT * FROM accounts WHERE name LIKE 'Scanner · Gewichte%'")
    assert v and json.loads(v["params"])["weights"]["momentum"] == rep["neu"]["momentum"]
    assert e.learner.learn_weights() is False      # zweites Konto erst nach 3 Tagen


def test_shadow_api(store):
    import app as appmod
    from tests.test_learning_app import auth
    r = appmod.app.test_client().get("/api/shadow?days=7", headers=auth())
    assert r.status_code == 200 and "gewichte" in r.json and "launches" in r.json
