"""v1.5: neue Runde fürs Hauptkonto, Verlierer aus, Backtest-Fokus."""
import json
import time

from memebot import config, params
from memebot.engine import Engine
from tests.test_engine import FakeMarket, pair
from tests.test_v12 import _record


def test_new_round(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    Engine(store)
    old = store.one("SELECT * FROM accounts WHERE kind='main' AND strategy='scanner'")
    store.x("INSERT INTO positions(account_id,mint,status,created,cost,budget) VALUES(?,?,?,?,?,?)", (old["id"], "O", "open", 1, 5, 5))
    store.x("UPDATE accounts SET cash=200 WHERE id=?", (old["id"],))
    store.kv_set("v15_neue_runde", False)
    e = Engine(store)
    arch = store.one("SELECT * FROM accounts WHERE id=?", (old["id"],))
    assert arch["kind"] == "archiv" and arch["active"] == 0 and "Runde 1" in arch["name"]
    assert store.one("SELECT status FROM positions WHERE mint='O'")["status"] == "cancelled"
    main = [a for a in e.trader.accounts("scanner") if a["kind"] == "main"]
    assert len(main) == 1 and main[0]["cash"] == config.START_BALANCE
    p = main[0]["params"]
    assert p["exclude_curve"] == 1 and p["min_h1_change"] == 30 and p["rug_watch"] == 1 and not p.get("paused")
    names = {a["name"] for a in e.trader.accounts("scanner")}
    assert "Scanner · Vorsichtig" not in names
    # neues Hauptkonto handelt einen migrierten Rücksetzer
    d = pair("POOLN", "MINTN", "NEW", 0.0001, ch_m5=-8, ch_h1=60, buys=60, sells=50)
    d["dex"] = "pumpswap"
    fm.add(d)
    e.cycle()
    assert e.trader.open_positions(main[0]["id"])
    # zweiter Start: nichts passiert noch einmal
    Engine(store)
    assert store.val("SELECT COUNT(*) FROM accounts WHERE kind='archiv'") == 1


def test_fresh_db_untouched(store, monkeypatch):
    FakeMarket(monkeypatch)
    e = Engine(store)
    assert not store.one("SELECT * FROM accounts WHERE kind='archiv'")
    assert "Scanner · Vorsichtig" in {a["name"] for a in e.trader.accounts("scanner")}


def test_backtest_focus_and_multiple_accounts(store, monkeypatch):
    FakeMarket(monkeypatch)
    e = Engine(store)
    start = time.time() - 30 * 3600
    for k in range(12):
        prices = [0.001 * (1.003 ** i) for i in range(300)]
        _record(store, f"MINT{k:02d}", prices, start + k * 2.4 * 3600, liq=6000)
    monkeypatch.setattr(config, "BACKTEST_MIN_TRADES", 3)
    calls = []
    orig = e.backtester.rng.choice

    def spy(pool):
        if pool and isinstance(pool[0], dict):
            calls.append(pool)
        return orig(pool)
    monkeypatch.setattr(e.backtester.rng, "choice", spy)
    summary, ranked = e.backtester.run("test", hours=32, n_variants=30)
    assert calls and all(all(x.get("min_h1_change", -100) >= 10 for x in pool) for pool in calls)
    n = store.val("SELECT COUNT(*) FROM accounts WHERE name LIKE 'Scanner · Replay%'")
    assert n <= config.BACKTEST_MAX_NEW
    if summary["testkonto"]:
        assert n == len(summary["testkonto"].split(", "))
    assert params.VARIANTS["scanner"]["Migriert + Rücksetzer"]["exclude_curve"] == 1
    assert json.loads(store.one("SELECT summary FROM backtests ORDER BY id DESC")["summary"])
