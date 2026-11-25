"""v1.5.1: keine Käufe zu Fantasiekursen (Kursfehler / riesige Slippage), Phantom-Trades herausrechnen."""
from memebot import config
from memebot.engine import Engine
from tests.test_engine import FakeMarket, main_acc, pair


def _order(e, fm, **kw):
    d = pair("POOLX", "MINTX", "GLITCH", 0.0001, **kw)
    fm.add(d)
    e.cycle()   # Order
    pos = e.trader.open_positions(main_acc(e)["id"])
    assert pos and pos[0]["status"] == "pending"
    return d, pos[0]


def test_crash_before_fill_is_not_bought(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    e = Engine(store)
    d, pos = _order(e, fm)
    fm.p["POOLX"]["price"] = 0.0001 / 100      # Kurs "fällt" um 99 % – typischer Datenfehler
    e.cycle()
    p = store.one("SELECT status FROM positions WHERE id=?", (pos["id"],))
    assert p["status"] == "pending"            # nicht zum Fantasiekurs gekauft, sondern abgewartet
    fm.p["POOLX"]["price"] = 0.0001             # Kurs wieder normal -> Kauf zum echten Kurs
    for _ in range(3):                           # Sprung ×100 nach oben glaubt der Scanner erst nach 3 Bestätigungen
        e.cycle()
    p = store.one("SELECT status, market_entry FROM positions WHERE id=?", (pos["id"],))
    assert p["status"] == "open" and abs(p["market_entry"] - 0.0001) < 1e-9


def test_huge_slippage_cancels(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    e = Engine(store)
    d, pos = _order(e, fm)
    fm.p["POOLX"]["reserve"] = 5.0              # Pool praktisch leer -> Swap mit irrer Slippage würde scheitern
    e.cycle()
    p = store.one("SELECT status, reason_out FROM positions WHERE id=?", (pos["id"],))
    assert p["status"] == "cancelled" and "Slippage" in p["reason_out"]


def test_phantom_trade_removed(store, monkeypatch):
    FakeMarket(monkeypatch)
    e = Engine(store)
    acc = main_acc(e)
    store.x("""INSERT INTO positions(account_id,mint,symbol,status,created,opened_at,closed_at,cost,budget,proceeds)
               VALUES(?,?,?,?,?,?,?,?,?,?)""", (acc["id"], "J", "Jensen", "closed", 100, 100, 200, 90, 90, 1310))
    pid = store.val("SELECT MAX(id) FROM positions")
    store.x("INSERT INTO fills(position_id,ts,side,market_price,eff_price,qty,usd,fee,slip_pct,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, 100, "buy", 1e-9, 1e-7, 1, 90, 1, 8938.7, ""))
    store.x("UPDATE accounts SET cash=? WHERE id=?", (acc["cash"] + 1220, acc["id"]))
    store.x("INSERT INTO equity VALUES(?,?,?)", (acc["id"], 150, 3000))
    store.x("INSERT INTO equity VALUES(?,?,?)", (acc["id"], 250, 2220))
    store.kv_set("v151_phantom", False)
    e = Engine(store)
    acc2 = main_acc(e)
    assert abs(acc2["cash"] - acc["cash"]) < 0.01
    assert store.one("SELECT status FROM positions WHERE id=?", (pid,))["status"] == "ungueltig"
    eq = store.q("SELECT ts, equity FROM equity WHERE account_id=? ORDER BY ts", (acc["id"],))
    assert [r["ts"] for r in eq] == [250] and abs(eq[0]["equity"] - 1000) < 0.01
    stats = e.trader.account_stats(acc2)
    assert stats["trades"] == 0
    Engine(store)   # zweiter Start ändert nichts mehr
    assert abs(main_acc(e)["cash"] - acc["cash"]) < 0.01


def test_backtest_skips_impossible_fill(monkeypatch):
    from memebot import backtest

    class SR:
        n = 3
        price = [1e-9, 1e-9, 1e-9]
        reserve = [5.0, 5.0, 5.0]
    assert backtest._forward({}, SR, 0, 90, 15) is None
    assert config.MAX_FILL_SLIP_PCT == 30
