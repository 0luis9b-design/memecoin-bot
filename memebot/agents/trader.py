"""Sizer, Executor und Risk-Guard – das Spielgeld-Depot jedes Kontos.

Sizer     Einsatz nach Punktzahl (z. B. 4–10 % vom Konto), begrenzt durch eigene Kursbewegung, Kasse und Gesamtanteil im Markt.
Executor  Order wartet FILL_DELAY_SEC und wird dann zum DANN gültigen Kurs mit Gebühren und Slippage ausgeführt.
          Ist der Kurs bis dahin zu weit weggelaufen, wird die Order verworfen.
Risk      Stop-Loss, Teilverkauf, Trailing-Stop, Höchst-Haltedauer, Rug-Alarm (Liquidität bricht ein / Pool weg).
"""
import json
import time

from .. import config, notify, sim
from ..notify import esc

MAX_FILL_DRIFT_PCT = 25     # Kurs bis zur Ausführung um mehr als 25 % gestiegen -> Order verwerfen
PENDING_TIMEOUT_SEC = 180   # keine frischen Kurse -> Order verwerfen


class Trader:
    name = "trader"

    def __init__(self, store):
        self.s = store
        self.stats = {"orders": 0, "ausgefuehrt": 0, "verworfen": 0, "verkaeufe": 0}
        self._skip_seen = {}

    def _skip(self, agent, acc, sig, why):
        """Ablehnung loggen – je Konto und Coin höchstens alle 10 min (sonst bei jedem Kurs-Update)."""
        k = (acc["id"], sig["mint"], why[:20])
        if time.time() - self._skip_seen.get(k, 0) < 600:
            return
        if len(self._skip_seen) > 20000:
            self._skip_seen.clear()
        self._skip_seen[k] = time.time()
        self.s.decide(agent, "SKIP", f"{acc['name']}: {why}", acc["id"], sig["mint"], sig["symbol"])

    # ------------------------------------------------------------ Konten
    def accounts(self, strategy=None):
        sql, args = "SELECT * FROM accounts WHERE active=1", []
        if strategy:
            sql += " AND strategy=?"
            args.append(strategy)
        rows = self.s.q(sql + " ORDER BY kind DESC, id", args)
        for a in rows:
            a["params"] = json.loads(a["params"])
        return rows

    def open_positions(self, account_id=None):
        sql, args = "SELECT * FROM positions WHERE status IN ('pending','open')", []
        if account_id is not None:
            sql += " AND account_id=?"
            args.append(account_id)
        return self.s.q(sql, args)

    def equity(self, acc):
        pos = self.open_positions(acc["id"])
        return acc["cash"] + sum((p["value"] or 0) if p["status"] == "open" else p["budget"] for p in pos)

    # ------------------------------------------------------------ Sizer + Order
    def size(self, acc, score, reserve):
        p = acc["params"]
        eq = self.equity(acc)
        span = max(1.0, 100 - p["min_score"])
        pct = p["size_min_pct"] + (p["size_max_pct"] - p["size_min_pct"]) * max(0.0, min(1.0, (score - p["min_score"]) / span))
        usd = eq * pct / 100
        limit_impact = sim.max_usd_for_impact(p["max_impact_pct"], reserve)
        exposure = eq - acc["cash"]
        room = eq * p["max_exposure_pct"] / 100 - exposure
        usd = min(usd, limit_impact, acc["cash"], room)
        return round(usd, 2), pct, limit_impact

    def try_enter(self, acc, sig, agent):
        """sig: mint, pool, symbol, dex, score, price, reserve, reason, data"""
        p = acc["params"]
        pos = self.open_positions(acc["id"])
        if any(x["mint"] == sig["mint"] for x in pos):
            return None
        if len(pos) >= p["max_positions"]:
            self._skip(agent, acc, sig, f"schon {len(pos)} Positionen offen")
            return None
        last = self.s.val("SELECT MAX(closed_at) FROM positions WHERE account_id=? AND mint=? AND status='closed'",
                          (acc["id"], sig["mint"]))
        if last and time.time() - last < p["reentry_min"] * 60:
            return None
        usd, pct, limit_impact = self.size(acc, sig["score"], sig["reserve"])
        if usd < 5:
            why = "Kasse/Marktanteil ausgeschöpft" if limit_impact >= 5 else "Pool zu klein für einen sinnvollen Einsatz"
            self._skip(agent, acc, sig, why)
            return None
        now = time.time()
        self.s.x("UPDATE accounts SET cash=cash-? WHERE id=?", (usd, acc["id"]))
        acc["cash"] -= usd
        cur = self.s.x("""INSERT INTO positions(account_id,mint,pool,symbol,dex,status,created,budget,cost,qty,qty_left,
                          entry_price,market_entry,peak_price,last_price,value,score,reason_in,signal,entry_liq)
                          VALUES(?,?,?,?,?,'pending',?,?,?,0,0,0,?,0,?,0,?,?,?,?)""",
                       (acc["id"], sig["mint"], sig["pool"], sig["symbol"], sig["dex"], now, usd, usd, sig["price"],
                        sig["price"], sig["score"], sig["reason"][:300], json.dumps(sig.get("data"), default=str)[:8000],
                        sig["reserve"]))
        self.stats["orders"] += 1
        self.s.decide("sizer", "ORDER", f"{acc['name']}: {usd:.2f} $ ({usd / max(1e-9, self.equity(acc)) * 100:.1f} % vom Konto, "
                      f"Ziel {pct:.1f} %) bei Punktzahl {sig['score']:.0f}", acc["id"], sig["mint"], sig["symbol"],
                      {"usd": usd, "ziel_pct": pct, "max_wegen_kursbewegung": round(limit_impact, 2)})
        return cur.lastrowid

    # ------------------------------------------------------------ Ausführen + Risiko
    def process(self, updated):
        """Nach jedem Kurs-Update: offene Orders ausführen, Positionen bewerten, Ausstiegsregeln prüfen."""
        now = time.time()
        accs = {a["id"]: a for a in self.accounts()}
        tok = {}
        for pos in self.open_positions():
            acc = accs.get(pos["account_id"])
            if not acc:
                continue
            d = updated.get(pos["mint"])
            if pos["status"] == "pending":
                self._fill(pos, acc, d, now)
                continue
            if d is None:
                if pos["mint"] not in tok:
                    tok[pos["mint"]] = self.s.one("SELECT missing_since FROM tokens WHERE mint=?", (pos["mint"],)) or {}
                ms = tok[pos["mint"]].get("missing_since")
                if ms and now - ms > config.MISSING_RUG_MIN * 60:
                    self._close_worthless(pos, acc, f"Pool seit {config.MISSING_RUG_MIN:.0f} min verschwunden – als Rug gewertet")
                continue
            self._risk(pos, acc, d, now)

    def _fill(self, pos, acc, d, now):
        if now - pos["created"] > PENDING_TIMEOUT_SEC:
            return self._cancel(pos, acc, "keine frischen Kurse – Order verworfen")
        if d is None or now - pos["created"] < config.FILL_DELAY_SEC:
            return None
        drift = (d["price"] / pos["market_entry"] - 1) * 100 if pos["market_entry"] else 0
        if drift > MAX_FILL_DRIFT_PCT:
            return self._cancel(pos, acc, f"Kurs bis zur Ausführung um {drift:.0f} % gestiegen – zu teuer")
        qty, eff, fee, imp = sim.buy(pos["budget"], d["price"], d["reserve"])
        if qty <= 0:
            return self._cancel(pos, acc, "Ausführung nicht möglich")
        slip = (eff / d["price"] - 1) * 100
        value, _, _ = sim.sell(qty, d["price"], d["reserve"])
        self.s.x("""UPDATE positions SET status='open', opened_at=?, qty=?, qty_left=?, entry_price=?, market_entry=?,
                    peak_price=?, last_price=?, value=?, entry_liq=? WHERE id=?""",
                 (now, qty, qty, eff, d["price"], d["price"], d["price"], value, d["reserve"], pos["id"]))
        self.s.x("INSERT INTO fills(position_id,ts,side,market_price,eff_price,qty,usd,fee,slip_pct,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (pos["id"], now, "buy", d["price"], eff, qty, pos["budget"], fee, slip,
                  f"Verzögerung {now - pos['created']:.0f} s, Kursbewegung durch Kauf {imp:.2f} %"))
        self.stats["ausgefuehrt"] += 1
        self.s.decide("executor", "ENTER", f"{acc['name']}: gekauft für {pos['budget']:.2f} $ – Slippage inkl. Gebühren {slip:.1f} %, "
                      f"Kurs seit Signal {drift:+.1f} %", acc["id"], pos["mint"], pos["symbol"],
                      {"position": pos["id"], "kurs": d["price"], "effektiv": eff, "gebuehr": fee})
        if acc["kind"] == "main":
            notify.send("normal", f"Gekauft {pos['symbol']} ({acc['name']})",
                        f"{pos['budget']:.2f} $ · Punktzahl {pos['score']:.0f}\n{esc(pos['reason_in'])}\nMarktkap. {d['mcap']:,.0f} $")
        return True

    def _cancel(self, pos, acc, why):
        self.s.x("UPDATE positions SET status='cancelled', closed_at=?, reason_out=? WHERE id=?", (time.time(), why, pos["id"]))
        self.s.x("UPDATE accounts SET cash=cash+? WHERE id=?", (pos["budget"], acc["id"]))
        acc["cash"] += pos["budget"]
        self.stats["verworfen"] += 1
        self.s.decide("executor", "CANCEL", f"{acc['name']}: {why}", acc["id"], pos["mint"], pos["symbol"])
        return False

    def _risk(self, pos, acc, d, now):
        p = acc["params"]
        price = d["price"]
        peak = max(pos["peak_price"] or 0, price)
        value, _, _ = sim.sell(pos["qty_left"], price, d["reserve"])
        self.s.x("UPDATE positions SET last_price=?, peak_price=?, value=? WHERE id=?", (price, peak, value, pos["id"]))
        pos.update(last_price=price, peak_price=peak, value=value)
        ent = pos["market_entry"]
        gain = (price / ent - 1) * 100
        held = (now - pos["opened_at"]) / 60
        if pos["entry_liq"] and d["reserve"] < pos["entry_liq"] * 0.3:
            return self.exit(pos, acc, 1.0, d, f"Rug-Alarm: Liquidität von {pos['entry_liq']:,.0f} $ auf {d['reserve']:,.0f} $ eingebrochen")
        if gain <= -p["stop_pct"]:
            return self.exit(pos, acc, 1.0, d, f"Stop-Loss bei {gain:+.1f} % (Grenze −{p['stop_pct']} %)")
        if not pos["tp1_done"] and gain >= p["tp1_pct"]:
            self.exit(pos, acc, p["tp1_sell_pct"] / 100, d, f"Teilverkauf {p['tp1_sell_pct']} % bei {gain:+.0f} %", partial=True)
            return True
        peak_gain = (peak / ent - 1) * 100
        if peak_gain >= p["trail_start_pct"] and price <= peak * (1 - p["trail_pct"] / 100):
            return self.exit(pos, acc, 1.0, d, f"Trailing-Stop: {p['trail_pct']} % unter Hoch (+{peak_gain:.0f} %), jetzt {gain:+.0f} %")
        if held >= p["max_hold_min"]:
            return self.exit(pos, acc, 1.0, d, f"Höchst-Haltedauer {p['max_hold_min']} min erreicht ({gain:+.0f} %)")
        return False

    def exit(self, pos, acc, fraction, d, reason, partial=False, agent="risk"):
        now = time.time()
        qty = pos["qty_left"] * min(1.0, fraction)
        if fraction >= 0.999 or pos["qty_left"] - qty < pos["qty"] * 0.01:
            qty = pos["qty_left"]
        usd, eff, fee = sim.sell(qty, d["price"], d["reserve"])
        left = pos["qty_left"] - qty
        proceeds = (pos["proceeds"] or 0) + usd
        self.s.x("UPDATE accounts SET cash=cash+? WHERE id=?", (usd, acc["id"]))
        acc["cash"] += usd
        self.s.x("INSERT INTO fills(position_id,ts,side,market_price,eff_price,qty,usd,fee,slip_pct,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (pos["id"], now, "sell", d["price"], eff, qty, usd, fee, (1 - eff / d["price"]) * 100 if d["price"] else 0, reason))
        self.stats["verkaeufe"] += 1
        if left <= 0:
            pnl = proceeds - pos["cost"]
            self.s.x("UPDATE positions SET status='closed', closed_at=?, qty_left=0, value=0, proceeds=?, reason_out=? WHERE id=?",
                     (now, proceeds, reason, pos["id"]))
            self.s.decide(agent, "EXIT", f"{acc['name']}: {reason} → {pnl:+.2f} $ ({pnl / pos['cost'] * 100:+.0f} %)",
                          acc["id"], pos["mint"], pos["symbol"], {"position": pos["id"], "pnl": pnl})
            if acc["kind"] == "main":
                notify.send("normal", f"{'✅' if pnl >= 0 else '❌'} Verkauft {pos['symbol']} ({acc['name']})",
                            f"{pnl:+.2f} $ ({pnl / pos['cost'] * 100:+.0f} %)\n{esc(reason)}")
        else:
            value, _, _ = sim.sell(left, d["price"], d["reserve"])
            self.s.x("UPDATE positions SET qty_left=?, proceeds=?, value=?, tp1_done=? WHERE id=?",
                     (left, proceeds, value, 1 if partial else pos["tp1_done"], pos["id"]))
            pos.update(qty_left=left, proceeds=proceeds, value=value, tp1_done=1 if partial else pos["tp1_done"])
            self.s.decide(agent, "PARTIAL", f"{acc['name']}: {reason} → {usd:.2f} $ gesichert", acc["id"], pos["mint"],
                          pos["symbol"], {"position": pos["id"]})
        return True

    def _close_worthless(self, pos, acc, reason):
        now = time.time()
        pnl = (pos["proceeds"] or 0) - pos["cost"]
        self.s.x("UPDATE positions SET status='closed', closed_at=?, qty_left=0, value=0, reason_out=? WHERE id=?",
                 (now, reason, pos["id"]))
        self.s.x("INSERT INTO fills(position_id,ts,side,market_price,eff_price,qty,usd,fee,slip_pct,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (pos["id"], now, "sell", 0, 0, pos["qty_left"], 0, 0, 100, reason))
        self.s.decide("risk", "EXIT", f"{acc['name']}: {reason} → {pnl:+.2f} $", acc["id"], pos["mint"], pos["symbol"],
                      {"position": pos["id"], "pnl": pnl})
        if acc["kind"] == "main":
            notify.send("normal", f"💀 Rug {pos['symbol']} ({acc['name']})", f"{pnl:+.2f} $\n{esc(reason)}")

    # ------------------------------------------------------------ Kennzahlen
    def record_equity(self):
        now = time.time()
        rows = [(a["id"], now, round(self.equity(a), 2)) for a in self.accounts()]
        self.s.many("INSERT INTO equity VALUES(?,?,?)", rows)

    def account_stats(self, acc, since=None):
        sql = "SELECT * FROM positions WHERE account_id=? AND status='closed'"
        args = [acc["id"]]
        if since:
            sql += " AND opened_at>=?"
            args.append(since)
        closed = self.s.q(sql, args)
        pnls = [(c["proceeds"] or 0) - c["cost"] for c in closed]
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x <= 0]
        eq_rows = self.s.q("SELECT equity FROM equity WHERE account_id=? ORDER BY ts", (acc["id"],))
        peak, mdd = acc["start_balance"], 0.0
        for r in eq_rows:
            peak = max(peak, r["equity"])
            mdd = max(mdd, (peak - r["equity"]) / peak * 100 if peak else 0)
        eq = self.equity(acc)
        best = max(closed, key=lambda c: (c["proceeds"] or 0) - c["cost"], default=None)
        return {
            "equity": round(eq, 2), "start": acc["start_balance"], "pnl": round(eq - acc["start_balance"], 2),
            "pnl_pct": round((eq / acc["start_balance"] - 1) * 100, 1), "multiple": round(eq / acc["start_balance"], 2),
            "trades": len(closed), "wins": len(wins), "losses": len(losses),
            "winrate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) < 0 else (None if not wins else 99.0),
            "realized": round(sum(pnls), 2), "max_drawdown": round(mdd, 1),
            "open": len(self.open_positions(acc["id"])), "cash": round(acc["cash"], 2),
            "best": {"symbol": best["symbol"], "pnl": round((best["proceeds"] or 0) - best["cost"], 2)} if best else None,
        }
