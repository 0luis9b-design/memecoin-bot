"""v1.4: Rug-Wächter, Smart-Money-Bestätigung, Sniper-Merkmal, Einsatz auf der Kurve."""
import json
import time

from memebot import config, params, rules
from memebot.agents import analysis
from memebot.agents.rugwatch import compare
from memebot.engine import Engine
from tests.test_engine import GOOD_RUG, FakeMarket, pair

BASE = dict(GOOD_RUG, creator_pct=4.0, top_holders=[["WAL1", 8.0], ["WAL2", 2.0]], holders=400, lp_locked_pct=100)


def test_compare_finds_warning_signs():
    assert compare(BASE, dict(BASE)) == []
    assert "Ersteller verkauft" in compare(BASE, dict(BASE, creator_pct=1.0))[0]
    assert "großer Holder" in compare(BASE, dict(BASE, top_holders=[["WAL2", 2.0]]))[0]
    assert compare(BASE, dict(BASE, top_holders=[["WAL1", 7.0]])) == []            # kleine Bewegung: kein Alarm
    assert "Holder fallen" in compare(BASE, dict(BASE, holders=300))[0]
    assert "entsperrt" in compare(BASE, dict(BASE, lp_locked_pct=20))[0]
    w = compare(BASE, dict(BASE, risks=[{"name": "Large Amount of LP Unlocked", "level": "danger"}]))
    assert "neue Gefahr-Warnung" in w[0]
    assert compare(None, BASE) == []


def accs(e):
    return {a["name"]: a for a in e.trader.accounts("scanner")}


def test_rugwatch_sells_only_where_enabled(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    monkeypatch.setattr(config, "RUGWATCH_SEC", 0)
    e = Engine(store)
    fm.rug["MINTR"] = dict(BASE)
    fm.add(pair("POOLR", "MINTR", "WATCH", 0.0001, ch_m5=10, ch_h1=40))
    e.cycle(); e.cycle()
    a = accs(e)
    main_pos = e.trader.open_positions(a["Scanner"]["id"])
    test_pos = e.trader.open_positions(a["Scanner · Kein Hinterherrennen"]["id"])
    assert main_pos and test_pos and test_pos[0]["status"] == "open"
    fm.rug["MINTR"] = dict(BASE, top_holders=[["WAL2", 2.0]])      # Wal hat verkauft
    e.due.pop("rugwatch", None)
    e.cycle()
    closed = store.one("SELECT * FROM positions WHERE account_id=? AND status='closed'", (a["Scanner · Kein Hinterherrennen"]["id"],))
    assert closed and closed["reason_out"].startswith("Rug-Wächter")
    assert e.trader.open_positions(a["Scanner"]["id"])                  # Hauptkonto: Wächter aus (erst nach Freigabe)
    assert store.one("SELECT * FROM shadow WHERE rule='rugwaechter'")


def test_smart_money_confirmation(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    e = Engine(store)
    fm.add(pair("POOLS", "MINTS", "SMART", 0.0001, ch_m5=10, ch_h1=40))
    e.cycle()
    a = accs(e)
    assert not e.trader.open_positions(a["Scanner · Smart-Money bestätigt"]["id"])
    store.x("INSERT INTO wallets(address,score,tracked,tokens) VALUES('SW1',0.7,1,5)")
    store.x("INSERT INTO wallet_trades VALUES('SW1','MINTS',?,'buy',100,0.0001,'tx1','gt')", (time.time() - 600,))
    e.trader._skip_seen.clear()
    fm.p["POOLS"]["price"] *= 1.001
    e.cycle()
    pos = e.trader.open_positions(a["Scanner · Smart-Money bestätigt"]["id"])
    assert pos and json.loads(pos[0]["signal"])["smart_wallets"] == 1


def test_sniper_feature(store, monkeypatch):
    FakeMarket(monkeypatch)
    e = Engine(store)
    created = time.time() - 600
    rows = [(f"S{i}", "MSN", created + 2, "buy", 50, 0.001, f"t{i}", "gt") for i in range(6)]
    rows.append(("LATE", "MSN", created + 120, "buy", 50, 0.001, "tl", "gt"))
    store.many("INSERT INTO wallet_trades VALUES(?,?,?,?,?,?,?,?)", rows)
    sn = e.scout.sniper("MSN", created)
    assert sn == {"kaeufer": 6, "usd": 300}
    assert e.scout.sniper("MSN", created - 3600) is None               # Trades decken den Start nicht ab
    d = dict(pair("P", "MSN", "S", 0.001), age_min=10, sniper=sn)
    v = analysis.components(d, [], dict(GOOD_RUG))["kein_sniper"][0]
    assert 0 < v < 0.5
    assert analysis.WEIGHTS["kein_sniper"] == 0


def test_curve_size_and_migration(store, monkeypatch):
    p = dict(params.defaults("scanner"), curve_size_mult=0.5)
    assert rules.venue_mult(p, "pumpfun") == 0.5 and rules.venue_mult(p, "pumpswap") == 1.0
    assert rules.venue_mult(params.defaults("scanner"), "pumpfun") == 1.0
    FakeMarket(monkeypatch)
    Engine(store)
    store.x("INSERT INTO accounts(name,strategy,kind,params,start_balance,cash,created,active) VALUES(?,?,?,?,?,?,?,1)",
            ("Scanner · Alt", "scanner", "test", json.dumps(params.defaults("scanner")), 1000, 1000, time.time()))
    store.kv_set("v14_schutz", False)
    Engine(store)
    alt = json.loads(store.one("SELECT params FROM accounts WHERE name='Scanner · Alt'")["params"])
    assert alt["rug_watch"] == 1 and alt["curve_size_mult"] == 0.5
    main = json.loads(store.one("SELECT params FROM accounts WHERE name='Scanner'")["params"])
    assert not main.get("rug_watch")


def test_main_account_paused(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    e = Engine(store)
    a = accs(e)
    assert not a["Scanner"]["params"].get("paused")                  # neue Datenbank: nicht pausiert
    store.x("INSERT INTO positions(account_id,mint,status,created,cost) VALUES(?,?,?,?,?)", (a["Scanner"]["id"], "OLD", "closed", 1, 1))
    store.kv_set("v141_haupt_pausiert", False)
    e = Engine(store)
    fm.add(pair("POOLP", "MINTP", "PAUSE", 0.0001, ch_m5=10, ch_h1=40))
    e.cycle()
    a = accs(e)
    assert a["Scanner"]["params"]["paused"] == 1
    assert not e.trader.open_positions(a["Scanner"]["id"])
    assert e.trader.open_positions(a["Scanner · Kein Hinterherrennen"]["id"])
