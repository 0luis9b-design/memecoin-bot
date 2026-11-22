import base64
import json
import time

import pytest

from memebot import config
from memebot.engine import Engine


def add_trades(store, acc_id, n, pnl):
    now = time.time()
    for i in range(n):
        store.x("""INSERT INTO positions(account_id,mint,pool,symbol,status,created,opened_at,closed_at,budget,cost,qty,qty_left,
                   proceeds,value,market_entry) VALUES(?,?,?,?,'closed',?,?,?,50,50,1,0,?,0,1)""",
                (acc_id, f"M{acc_id}-{i}", "P", "T", now, now, now, 50 + pnl))
    store.x("UPDATE accounts SET cash=cash+? WHERE id=?", (n * pnl, acc_id))


def test_proposal_needs_approval_and_weak_variant_is_replaced(store):
    e = Engine(store)
    accs = e.trader.accounts("scanner")
    main = [a for a in accs if a["kind"] == "main"][0]
    tests = [a for a in accs if a["kind"] == "test"]
    good, bad = tests[0], tests[1]
    add_trades(store, good["id"], config.LEARN_MIN_TRADES, 10)    # +300 $
    add_trades(store, main["id"], 10, -2)
    add_trades(store, bad["id"], config.LEARN_MIN_TRADES, -8)     # −240 $
    e.learner.run()
    props = store.q("SELECT * FROM proposals WHERE status='offen'")
    assert len(props) == 1 and props[0]["variant_id"] == good["id"]
    # Hauptkonto unverändert, bis Luis freigibt
    assert json.loads(store.one("SELECT params FROM accounts WHERE id=?", (main["id"],))["params"]) == main["params"]
    ok, _ = e.learner.decide_proposal(props[0]["id"], True)
    assert ok
    assert json.loads(store.one("SELECT params FROM accounts WHERE id=?", (main["id"],))["params"]) == good["params"]
    # schwaches Testkonto abgeschaltet, neues mit abgewandelten Regeln da
    assert store.one("SELECT active FROM accounts WHERE id=?", (bad["id"],))["active"] == 0
    new = store.q("SELECT * FROM accounts WHERE kind='test' AND strategy='scanner' AND generation>=1")
    assert len(new) == 1 and new[0]["cash"] == config.START_BALANCE
    assert store.q("SELECT * FROM decisions WHERE action='MUTATE'")


def test_mutate_stays_in_bounds():
    import random
    from memebot import params
    rng = random.Random(1)
    p = params.defaults("scanner")
    for _ in range(200):
        p, _ = params.mutate(p, "scanner", rng)
        for k, (lo, hi, _) in params.MUTABLE.items():
            if k in p:
                assert lo <= p[k] <= hi
        assert p["trail_pct"] < p["trail_start_pct"]


@pytest.fixture
def client():
    import app as appmod
    return appmod.app.test_client()


def auth(u="luis", p="geheim"):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_login_required(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 401
    assert client.get("/api/overview", headers=auth("luis", "falsch")).status_code == 401
    r = client.get("/", headers=auth())
    assert r.status_code == 200 and b"MEMECOIN BOT" in r.data
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


@pytest.mark.parametrize("path", ["/api/overview", "/api/equity?hours=24", "/api/trades", "/api/log?q=x", "/api/wallets",
                                  "/api/learning", "/api/system"])
def test_api_ok(client, path):
    r = client.get(path, headers=auth())
    assert r.status_code == 200, r.data[:300]
    assert r.is_json


def test_post_needs_dashboard_header(client):
    r = client.post("/api/proposal/1", headers=auth(), json={"accept": True})
    assert r.status_code == 403
    r = client.post("/api/proposal/999", headers={**auth(), "X-Requested-With": "memebot"}, json={"accept": True})
    assert r.status_code == 400
