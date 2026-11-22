"""Ende-zu-Ende mit einem künstlichen Markt: Signal -> Order -> Ausführung -> Ausstieg."""
import json
import time

import pytest

from memebot import config, market
from memebot.agents import analysis, wallets
from memebot.engine import Engine

NOW = time.time()


def pair(pool, mint, sym, price, liq=40000, vol_m5=15000, vol_h1=40000, buys=150, sells=60, ch_m5=12, ch_h1=40, age_min=20):
    d = {"pool": pool, "mint": mint, "symbol": sym, "name": sym, "dex": "raydium", "quote": market.SOL_MINT, "price": price,
         "price_native": price / 150, "liq": liq, "mcap": price * 1e9, "created_at": time.time() - age_min * 60,
         "vol_m5": vol_m5, "vol_h1": vol_h1, "vol_h6": vol_h1, "buys_m5": buys, "sells_m5": sells, "buys_h1": buys * 3,
         "sells_h1": sells * 3, "ch_m5": ch_m5, "ch_h1": ch_h1, "ch_h6": ch_h1, "boosts": 0, "socials": 2}
    d["reserve"] = liq / 2
    return d


GOOD_RUG = {"mint_auth": False, "freeze_auth": False, "transfer_fee": 0, "rugged": False, "top10_pct": 15, "holders": 400,
            "insiders": 0, "insider_networks": 0, "creator_pct": 0, "creator_tokens": 1, "lp_locked_pct": 100, "score_norm": 1,
            "risks": [], "launchpad": "Pump.Fun"}


class FakeMarket:
    def __init__(self, monkeypatch):
        self.p = {}
        self.rug = {}
        self.listed = []
        mp = monkeypatch
        mp.setattr(market, "new_pools", lambda page=1: list(self.listed))
        mp.setattr(market, "trending_pools", lambda: [])
        mp.setattr(market, "ds_latest_lists", lambda: [])
        mp.setattr(market, "pairs", lambda pools: {k: dict(v) for k, v in self.p.items() if k in pools})
        mp.setattr(market, "rugcheck", lambda mint, pools=(): dict(self.rug.get(mint, GOOD_RUG)))
        mp.setattr(market, "pool_trades", lambda pool: [])
        mp.setattr(market, "best_pair_for_mint", lambda mints: {v["mint"]: dict(v) for v in self.p.values() if v["mint"] in mints})
        mp.setattr(analysis, "available", lambda src: 99)
        mp.setattr(wallets, "available", lambda src: 99)
        mp.setattr(wallets, "rpc", lambda *a, **k: [])
        for k, v in {"FILL_DELAY_SEC": 0, "REFRESH_HOT_SEC": 0, "REFRESH_ALL_SEC": 0, "MISSING_RUG_MIN": 0,
                     "SCAN_SEC": 0, "REPORT_HOUR": 99}.items():
            mp.setattr(config, k, v)

    def add(self, d):
        self.p[d["pool"]] = d
        self.listed.append({**d, "reserve": d["reserve"], "source": "neu"})


@pytest.fixture
def eng(store, monkeypatch):
    fm = FakeMarket(monkeypatch)
    e = Engine(store)
    return e, fm


def main_acc(e, strategy="scanner"):
    return [a for a in e.trader.accounts(strategy) if a["kind"] == "main"][0]


def test_accounts_created(eng):
    e, _ = eng
    accs = e.trader.accounts()
    assert {a["name"] for a in accs if a["kind"] == "main"} == {"Scanner", "Copy-Trading"}
    assert any(a["name"].endswith("TikTok") for a in accs)
    assert all(a["cash"] == config.START_BALANCE for a in accs)


def test_pump_then_trailing_stop_makes_money(eng):
    e, fm = eng
    fm.add(pair("POOL1", "MINT1", "PUMP", 0.0001))
    e.cycle()   # entdecken + bewerten -> Order
    pos = e.trader.open_positions(main_acc(e)["id"])
    assert pos and pos[0]["status"] == "pending", e.s.q("SELECT * FROM decisions")
    e.cycle()   # Ausführung zum dann gültigen Kurs
    pos = e.trader.open_positions(main_acc(e)["id"])[0]
    assert pos["status"] == "open" and pos["qty"] > 0
    fills = e.s.q("SELECT * FROM fills WHERE position_id=?", (pos["id"],))
    assert fills[0]["eff_price"] > 0.0001       # Slippage + Gebühren machen den Einstieg teurer
    for px in (0.00013, 0.00016, 0.00018):      # +80 %
        fm.p["POOL1"]["price"] = px
        e.cycle()
    fm.p["POOL1"]["price"] = 0.00014            # −22 % vom Hoch -> Trailing-Stop
    e.cycle()
    closed = e.s.q("SELECT * FROM positions WHERE account_id=? AND status='closed'", (main_acc(e)["id"],))
    assert len(closed) == 1
    c = closed[0]
    assert "Trailing" in c["reason_out"]
    assert c["proceeds"] > c["cost"]
    st = e.trader.account_stats(main_acc(e))
    assert st["trades"] == 1 and st["wins"] == 1 and st["equity"] > config.START_BALANCE


def test_stop_loss_and_cooldown(eng):
    e, fm = eng
    fm.add(pair("POOL2", "MINT2", "DUMP", 0.001))
    e.cycle(); e.cycle()
    fm.p["POOL2"]["price"] = 0.0008             # −20 %
    e.cycle()
    acc = main_acc(e)
    c = e.s.one("SELECT * FROM positions WHERE account_id=? AND status='closed'", (acc["id"],))
    assert c and "Stop-Loss" in c["reason_out"] and c["proceeds"] < c["cost"]
    fm.p["POOL2"]["price"] = 0.0009
    e.cycle()   # gleich danach nicht wieder kaufen (Sperre)
    assert not [p for p in e.trader.open_positions(acc["id"]) if p["mint"] == "MINT2"]


def test_rug_alarm_when_liquidity_vanishes(eng):
    e, fm = eng
    fm.add(pair("POOL3", "MINT3", "RUG", 0.001))
    e.cycle(); e.cycle()
    fm.p["POOL3"].update(liq=2000, reserve=1000, price=0.00095)
    e.cycle()
    c = e.s.one("SELECT * FROM positions WHERE account_id=? AND status='closed'", (main_acc(e)["id"],))
    assert c and "Rug-Alarm" in c["reason_out"]
    assert c["proceeds"] < c["cost"]            # Verkauf in den kleinen Rest-Pool kostet zusätzlich


def test_pool_disappears_counts_as_worthless(eng):
    e, fm = eng
    fm.add(pair("POOL4", "MINT4", "GONE", 0.001))
    e.cycle(); e.cycle()
    del fm.p["POOL4"]
    fm.listed.clear()
    e.cycle(); e.cycle()
    c = e.s.one("SELECT * FROM positions WHERE account_id=? AND status='closed'", (main_acc(e)["id"],))
    assert c and "verschwunden" in c["reason_out"] and (c["proceeds"] or 0) == 0


def test_rugcheck_blocks(eng):
    e, fm = eng
    fm.add(pair("POOL5", "MINT5", "SCAM", 0.001))
    fm.rug["MINT5"] = dict(GOOD_RUG, mint_auth=True, top10_pct=60)
    e.cycle(); e.cycle()
    assert not e.trader.open_positions()
    blocks = e.s.q("SELECT * FROM decisions WHERE action='BLOCK'")
    assert blocks and "Mint-Funktion" in blocks[0]["reason"]


def test_no_chasing_and_weak_buying_filtered(eng):
    e, fm = eng
    fm.add(pair("POOL6", "MINT6", "MOON", 0.001, ch_m5=400))           # schon +400 % in 5 min
    fm.add(pair("POOL7", "MINT7", "WEAK", 0.001, buys=40, sells=80))   # mehr Verkäufe als Käufe
    e.cycle(); e.cycle()
    assert not [p for p in e.trader.open_positions(main_acc(e)["id"])]


def test_sizer_respects_impact_and_positions(eng):
    e, fm = eng
    fm.add(pair("POOL8", "MINT8", "TINY", 0.001, liq=9000))
    e.cycle()
    p = e.trader.open_positions(main_acc(e)["id"])[0]
    from memebot import sim
    assert p["budget"] <= sim.max_usd_for_impact(2.5, 4500) + 0.01


def test_copy_signal_entry_and_follow_sell(eng, monkeypatch):
    e, fm = eng
    w = "SmartWallet111111111111111111111111111111111"
    e.s.x("INSERT INTO wallets(address,tokens,hits,misses,pnl_usd,score,tracked,last_sig) VALUES(?,?,?,?,?,?,1,'old')",
          (w, 6, 4, 1, 500, 0.7))
    d = pair("POOL9", "MINT9", "COPY", 0.001, age_min=60)
    fm.p["POOL9"] = d
    buy = [{"wallet": w, "mint": "MINT9", "kind": "buy", "price_usd": 0.00095, "usd": 200, "ts": time.time(), "sig": "s1", "wallet_score": 0.7}]
    e.handle_copy(buy)
    acc = main_acc(e, "copy")
    pos = e.trader.open_positions(acc["id"])
    assert pos and pos[0]["status"] == "pending" and w in pos[0]["signal"]
    e.cycle()
    assert e.trader.open_positions(acc["id"])[0]["status"] == "open"
    e.handle_copy([dict(buy[0], kind="sell", sig="s2")])
    c = e.s.one("SELECT * FROM positions WHERE account_id=? AND status='closed'", (acc["id"],))
    assert c and "verkauft" in c["reason_out"]


def test_copy_skips_when_too_expensive(eng):
    e, fm = eng
    w = "W2"
    fm.p["POOLA"] = pair("POOLA", "MINTA", "LATE", 0.002, age_min=60)
    e.handle_copy([{"wallet": w, "mint": "MINTA", "kind": "buy", "price_usd": 0.001, "usd": 100, "ts": time.time(),
                    "sig": "x", "wallet_score": 0.8}])
    assert not e.trader.open_positions(main_acc(e, "copy")["id"])
    assert e.s.q("SELECT * FROM decisions WHERE reason LIKE '%teurer als die Wallet%'")


def test_parse_swaps_buy_and_sell():
    w = "Wal1"
    tx = {"meta": {"err": None, "preBalances": [5_000_000_000, 0], "postBalances": [3_990_000_000, 0],
                   "preTokenBalances": [], "postTokenBalances": [{"owner": w, "mint": "MEME", "uiTokenAmount": {"uiAmount": 1000.0}}]},
          "transaction": {"message": {"accountKeys": [{"pubkey": w}, {"pubkey": "Pool"}]}}}
    sw = wallets.parse_swaps(tx, w)
    assert sw == [{"mint": "MEME", "kind": "buy", "qty": 1000.0, "sol": pytest.approx(1.01)}]
    tx2 = json.loads(json.dumps(tx))
    tx2["meta"].update(preBalances=[1_000_000_000, 0], postBalances=[3_000_000_000, 0],
                       preTokenBalances=[{"owner": w, "mint": "MEME", "uiTokenAmount": {"uiAmount": 1000.0}}],
                       postTokenBalances=[{"owner": w, "mint": "MEME", "uiTokenAmount": {"uiAmount": 0}}])
    assert wallets.parse_swaps(tx2, w)[0]["kind"] == "sell"
    tx["meta"]["err"] = {"x": 1}
    assert wallets.parse_swaps(tx, w) == []


def test_scout_finds_smart_wallet(store):
    s = wallets.Scout(store)
    old = time.time() - 3 * 3600
    rows = []
    for i in range(5):
        m = f"M{i}"
        store.x("INSERT INTO tokens(mint,pool,symbol,status,last_price,first_seen) VALUES(?,?,?,?,?,?)", (m, "P" + m, "T", "watch", 0.003, old))
        rows.append(("GOOD", m, old, "buy", 100, 0.001, f"g{i}", "gt"))          # gute Wallet: 3x
        rows.append(("BAD", m, old, "buy", 100, 0.005, f"b{i}", "gt"))           # schlechte Wallet: Verlust
        rows.append(("DUST", m, old, "buy", 2, 0.001, f"d{i}", "gt"))            # Kleinstbetrag
    store.many("INSERT INTO wallet_trades VALUES(?,?,?,?,?,?,?,?)", rows)
    s.score_wallets()
    w = {r["address"]: r for r in store.q("SELECT * FROM wallets")}
    assert w["GOOD"]["tracked"] == 1 and w["GOOD"]["hits"] == 5
    assert w["BAD"]["tracked"] == 0 and w["BAD"]["misses"] == 5
    assert "DUST" not in w
