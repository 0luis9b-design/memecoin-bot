"""Replay-Backtest: Regelsätze in Minuten statt Tagen prüfen.

Der Bot speichert laufend Kurs-Aufnahmen aller beobachteten Coins (Tabelle snapshots). Der Backtest spielt diese
Aufnahmen für jeden Regelsatz noch einmal ab – mit denselben Regeln wie im Live-Depot (rules.py, sim.py):
Vorfilter, Rug-Check, Punktzahl, Einsatzgröße, Verzögerung bis zur Ausführung, Gebühren/Slippage und alle Ausstiege.

Ehrlichkeit:
  * Train/Test: Regeln werden auf den ersten 70 % der Zeit ausgewählt und auf den letzten 30 % geprüft.
    Nur was auch im Test-Zeitraum besser ist, zählt – sonst passt man die Regeln an Zufall an.
  * Rug-Check nutzt den letzten bekannten RugCheck-Bericht des Coins (damals kann er anders ausgesehen haben).
  * Ausstiege werden an den aufgenommenen Punkten geprüft (alle 20 s bis 2 min) – live geht es alle 5 s.
  * Nur die Scanner-Strategie: Für Copy-Trading fehlen die Wallet-Ereignisse in den Aufnahmen.
"""
import heapq
import json
import math
from array import array
import logging
import random
import threading
import time
from collections import defaultdict

from . import config, params, rules, sim
from .agents import analysis
from .agents.analysis import RugGuard

log = logging.getLogger("memebot.backtest")
COLS = ("ts", "price", "mcap", "liq", "vol_m5", "buys_m5", "sells_m5", "ch_m5", "ch_h1", "vol_h1", "reserve", "boosts", "socials")


class Data:
    """Alle Aufnahmen und Kandidaten eines Zeitfensters (einmal laden, für viele Regelsätze nutzen)."""

    def __init__(self, store, hours):
        self.until = time.time()
        self.since = self.until - hours * 3600
        # Nur der Zeitraum, für den es vollständige Aufnahmen gibt – sonst wäre der Train-Teil leer
        first = store.val("SELECT MIN(ts) FROM snapshots WHERE ch_m5 IS NOT NULL AND ts>?", (self.since,))
        if first:
            self.since = max(self.since, first)
        mints = [r["mint"] for r in store.q(
            """SELECT DISTINCT mint FROM snapshots WHERE ts>? AND vol_m5>=500 AND buys_m5>=5 AND liq>=2000 AND ch_m5 IS NOT NULL""",
            (self.since,))]
        toks = {}
        for i in range(0, len(mints), 500):
            part = mints[i:i + 500]
            for t in store.q("SELECT mint, symbol, dex, created_at, first_seen, rug, holders_first, holders_first_at FROM tokens "
                             "WHERE mint IN (%s)" % ",".join("?" * len(part)), part):
                toks[t["mint"]] = t
        self.series = {}
        self.rug = {}
        self.meta = {}
        for mint in mints:
            t = toks.get(mint)
            if not t or not t["rug"]:
                continue
            rows = store.q("SELECT %s FROM snapshots WHERE mint=? AND ts>? ORDER BY ts" % ",".join(COLS), (mint, self.since))
            if len(rows) < 3:
                continue
            # Spalten als kompakte Zahlen-Arrays (8 Byte je Wert) – sonst wird der Speicher bei Millionen Aufnahmen knapp
            self.series[mint] = Series(rows)
            self.rug[mint] = json.loads(t["rug"])
            self.meta[mint] = {"symbol": t["symbol"], "dex": t["dex"], "created": t["created_at"] or t["first_seen"]}
        heat = store.q("SELECT ts, heat FROM regime WHERE ts>? ORDER BY ts", (self.since - 3600,))
        self.heat_ts = [h["ts"] for h in heat]
        self.heat_v = [h["heat"] for h in heat]
        self.candidates = self._candidates()

    def heat_at(self, ts):
        import bisect
        i = bisect.bisect_right(self.heat_ts, ts) - 1
        return self.heat_v[i] if i >= 0 else None

    def _candidates(self):
        """Zeitpunkte, an denen ein Kauf überhaupt in Frage kam (höchstens einer pro Coin und Minute)."""
        out = []
        for mint, sr in self.series.items():
            last = 0
            m = self.meta[mint]
            for i in range(sr.n):
                ts = sr.ts[i]
                if math.isnan(sr.ch_m5[i]) or ts - last < 60 or sr.vol_m5[i] < 500 or sr.buys_m5[i] < 5:
                    continue
                last = ts
                d = sr.row(i)
                d.update(mint=mint, symbol=m["symbol"], dex=m["dex"], age_min=(ts - m["created"]) / 60)
                k = i
                while k > 0 and ts - sr.ts[k - 1] <= 1800:
                    k -= 1
                window = list(sr.price[k:i + 1])
                comps = analysis.components(d, [{"price": x} for x in window], self.rug[mint])
                comps = {k: (v, "") for k, (v, _) in comps.items()}          # Erklärtexte sparen Speicher
                d = {k: d[k] for k in ("price", "liq", "reserve", "mcap", "vol_m5", "buys_m5", "sells_m5", "ch_m5", "ch_h1", "age_min", "dex")}
                out.append((ts, mint, i, d, comps, rules.swing_pct(window)))
        out.sort(key=lambda c: c[0])
        return out


class Series:
    """Aufnahmen eines Coins, spaltenweise."""
    __slots__ = ("n",) + COLS

    def __init__(self, rows):
        self.n = len(rows)
        for c in COLS:
            setattr(self, c, array("d", (float("nan") if r[c] is None else float(r[c]) for r in rows)))
        # fehlende Werte sinnvoll füllen
        for i in range(self.n):
            if math.isnan(self.reserve[i]):
                self.reserve[i] = (0.0 if math.isnan(self.liq[i]) else self.liq[i]) / 2
            for c in ("buys_m5", "sells_m5", "vol_h1", "ch_h1", "boosts", "socials", "liq", "mcap", "vol_m5"):
                col = getattr(self, c)
                if math.isnan(col[i]):
                    col[i] = 0.0

    def row(self, i):
        d = {c: getattr(self, c)[i] for c in COLS}
        d["buys_m5"], d["sells_m5"] = int(d["buys_m5"]), int(d["sells_m5"])
        return d


def _forward(p, sr, j, usd, stop):
    """Position ab Aufnahme j halten, bis eine Ausstiegsregel greift. -> (Ende-Zeit, Erlös, Grund)"""
    qty, eff, fee, imp = sim.buy(usd, sr.price[j], sr.reserve[j])
    if qty <= 0:
        return None
    st = {"market_entry": sr.price[j], "peak_price": sr.price[j], "opened_at": sr.ts[j], "tp1_done": 0,
          "entry_liq": sr.reserve[j], "stop_pct": stop, "flow_since": 0}
    left, proceeds = qty, 0.0
    for k in range(j + 1, sr.n):
        price, reserve = sr.price[k], sr.reserve[k]
        action, frac, reason, st["flow_since"] = rules.exit_decision(p, st, price, reserve, int(sr.buys_m5[k]),
                                                                       int(sr.sells_m5[k]), sr.ts[k])
        st["peak_price"] = max(st["peak_price"], price)
        if action == "partial":
            q = left * frac
            got, _, _ = sim.sell(q, price, reserve)
            proceeds += got
            left -= q
            st["tp1_done"] = 1
        elif action == "exit":
            got, _, _ = sim.sell(left, price, reserve)
            return sr.ts[k], proceeds + got, reason
    got, _, _ = sim.sell(left, sr.price[-1], sr.reserve[-1])
    return sr.ts[-1], proceeds + got, "Ende der Aufnahmen"


def run_variant(data, p, t_from, t_to, start=None):
    """Einen Regelsatz über einen Zeitraum abspielen. -> Kennzahlen"""
    start = start or config.START_BALANCE
    cash = start
    open_ = {}                 # mint -> Einsatz
    exits = []                 # (Zeit, mint, Erlös, Einsatz, Grund)
    cooldown = {}
    trades = []
    equity_pts = [start]
    weights = p.get("weights")
    for ts, mint, i, d, comps, swing in data.candidates:
        if ts < t_from or ts > t_to:
            continue
        while exits and exits[0][0] <= ts:
            _, m, got, cost, why = heapq.heappop(exits)
            cash += got
            open_.pop(m, None)
            trades.append((got - cost, cost, why))
            equity_pts.append(cash + sum(open_.values()))
        if mint in open_ or ts < cooldown.get(mint, 0) or len(open_) >= p["max_positions"]:
            continue
        ok, _, _ = analysis.check_pre(d, p)
        if not ok:
            continue
        if not all(c[1] for c in RugGuard.checks(data.rug[mint], p)):
            continue
        sc, _ = analysis.total(comps, weights)
        if sc < p["min_score"]:
            continue
        heat = data.heat_at(ts)
        mult = rules.regime_mult(p, heat)
        if mult is None:
            continue
        eq = cash + sum(open_.values())
        usd = eq * rules.size_pct(p, sc) * mult * rules.venue_mult(p, d.get("dex")) / 100
        usd = min(usd, sim.max_usd_for_impact(p["max_impact_pct"], d["reserve"]), cash,
                  eq * p["max_exposure_pct"] / 100 - sum(open_.values()))
        if usd < 5:
            continue
        sr = data.series[mint]
        j = next((k for k in range(i + 1, sr.n) if sr.ts[k] >= ts + config.FILL_DELAY_SEC), None)
        if j is None or sr.ts[j] - ts > 180 or sr.price[j] > d["price"] * 1.25:
            continue   # keine Ausführung möglich oder Kurs davongelaufen (wie live)
        res = _forward(p, sr, j, usd, rules.stop_for(p, swing))
        if not res:
            continue
        end_ts, got, why = res
        cash -= usd
        open_[mint] = usd
        heapq.heappush(exits, (end_ts, mint, got, usd, why))
        cooldown[mint] = end_ts + p["reentry_min"] * 60
    while exits:
        _, m, got, cost, why = heapq.heappop(exits)
        cash += got
        open_.pop(m, None)
        trades.append((got - cost, cost, why))
        equity_pts.append(cash + sum(open_.values()))
    wins = [t[0] for t in trades if t[0] > 0]
    losses = [t[0] for t in trades if t[0] <= 0]
    peak, mdd = start, 0.0
    for e in equity_pts:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100 if peak else 0)
    return {"rendite": round((cash / start - 1) * 100, 1), "trades": len(trades), "treffer": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "pf": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) < 0 else (99.0 if wins else None),
            "max_dd": round(mdd, 1), "gruende": _top_reasons(trades)}


def _top_reasons(trades):
    c = defaultdict(int)
    for _, _, why in trades:
        c[why.split(":")[0].split(" bei")[0].split(" nach")[0][:30]] += 1
    return dict(sorted(c.items(), key=lambda x: -x[1])[:4])


def mutate_wide(p, rng):
    """Für den Backtest dürfen auch die gezielten Regeln (Liquidität, Alter …) zufällig wandern."""
    out = dict(p)
    keys = [k for k in params.MUTABLE if k in out and k not in ("max_chase_pct", "min_wallet_score")]
    for k in rng.sample(keys, k=rng.randint(1, 3)):
        lo, hi, step = params.MUTABLE[k]
        v = max(lo, min(hi, out[k] + step * rng.choice([-2, -1, 1, 2])))
        out[k] = round(v, 2) if isinstance(step, float) else int(round(v))
    if out["trail_pct"] >= out["trail_start_pct"]:
        out["trail_pct"] = max(5, out["trail_start_pct"] - 2)
    return out


class Backtester:
    name = "backtest"

    def __init__(self, store, trader):
        self.s = store
        self.t = trader
        self.lock = threading.Lock()
        self.running = False
        self.progress = ""
        self.rng = random.Random()

    def start(self, reason="geplant"):
        """Im Hintergrund starten (blockiert den Takt nicht). False, wenn schon einer läuft."""
        if self.running:
            return False
        threading.Thread(target=self._safe_run, args=(reason,), name="backtest", daemon=True).start()
        return True

    def _safe_run(self, reason):
        try:
            self.run(reason)
        except Exception as e:  # noqa: BLE001
            log.exception("Backtest abgebrochen")
            self.s.x("INSERT INTO backtests(ts,status,summary,results) VALUES(?,?,?,?)",
                     (time.time(), "fehler", json.dumps({"fehler": f"{type(e).__name__}: {e}"[:300]}), "[]"))
        finally:
            self.running = False
            self.progress = ""

    def run(self, reason="manuell", hours=None, n_variants=None):
        with self.lock:
            self.running = True
            t0 = time.time()
            self.progress = "lade Aufnahmen …"
            data = Data(self.s, hours or config.BACKTEST_HOURS)
            split = data.since + (data.until - data.since) * 0.7
            accs = self.t.accounts("scanner")
            main = next(a for a in accs if a["kind"] == "main")
            sets = [("Hauptkonto (Scanner)", main["params"], "live")]
            sets += [(a["name"], a["params"], "live") for a in accs if a["kind"] == "test"]
            seen = {json.dumps(x[1], sort_keys=True) for x in sets}
            n = n_variants or config.BACKTEST_VARIANTS
            tries = 0
            while len(sets) < n + 1 + len(accs) - 1 and tries < n * 5:
                tries += 1
                base = self.rng.choice([main["params"]] + [a["params"] for a in accs if a["kind"] == "test"])
                v = mutate_wide(base, self.rng)
                key = json.dumps(v, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    sets.append((f"Variante {len(sets)}", v, "neu"))
            results = []
            for k, (name, p, kind) in enumerate(sets):
                self.progress = f"Regelsatz {k + 1}/{len(sets)}"
                tr = run_variant(data, p, data.since, split)
                te = run_variant(data, p, split, data.until)
                results.append({"name": name, "art": kind, "train": tr, "test": te, "params": p,
                                "aenderungen": {kk: list(vv) for kk, vv in params.diff(main["params"], p).items()}})
            base = results[0]
            for r in results:
                r["besser_im_test"] = round(r["test"]["rendite"] - base["test"]["rendite"], 1)
                r["robust"] = (r["train"]["trades"] >= config.BACKTEST_MIN_TRADES and r["test"]["trades"] >= max(3, config.BACKTEST_MIN_TRADES // 3)
                               and r["train"]["rendite"] > base["train"]["rendite"] and r["besser_im_test"] >= 5
                               and (r["test"]["pf"] or 0) >= 1.0
                               # „weniger schlecht“ reicht nicht: in BEIDEN Zeiträumen muss echter Gewinn herauskommen
                               and r["train"]["rendite"] > 0 and r["test"]["rendite"] > 0)
            ranked = [results[0]] + sorted(results[1:], key=lambda r: (r["robust"], r["train"]["rendite"]), reverse=True)
            summary = {"grund": reason, "stunden": round((data.until - data.since) / 3600, 1), "coins": len(data.series),
                       "kandidaten": len(data.candidates), "regelsaetze": len(results), "dauer_s": round(time.time() - t0, 1),
                       "train_bis": split, "von": data.since, "bis": data.until,
                       "robust": sum(1 for r in results if r["robust"] and r["art"] == "neu")}
            best = next((r for r in ranked[1:] if r["robust"] and r["art"] == "neu"), None)
            summary["testkonto"] = self._start_account(best, main) if best else None
            self.s.x("INSERT INTO backtests(ts,status,summary,results) VALUES(?,?,?,?)",
                     (time.time(), "fertig", json.dumps(summary), json.dumps(ranked[:25], default=str)))
            self.s.decide("learning", "INFO", f"Replay-Backtest: {len(results)} Regelsätze über {summary['stunden']} h, "
                          f"{summary['robust']} robust besser" + (f" – neues Testkonto {summary['testkonto']}" if summary["testkonto"] else ""))
            return summary, ranked

    def _start_account(self, best, main):
        """Bester robuster Regelsatz wird ein echtes Testkonto (höchstens eins alle 2 Tage, das ältere wird abgelöst)."""
        now = time.time()
        olds = [a for a in self.t.accounts("scanner") if a["name"].startswith("Scanner · Replay")]
        if any(now - a["created"] < 2 * 86400 for a in olds):
            return None
        for a in olds:
            self.s.x("UPDATE accounts SET active=0, note=? WHERE id=?", ("abgelöst durch neueren Replay-Regelsatz", a["id"]))
            self.s.x("UPDATE positions SET status='cancelled', reason_out='Testkonto abgeschaltet' WHERE account_id=? AND status IN ('pending','open')",
                     (a["id"],))
        n = 1 + self.s.val("SELECT COUNT(*) FROM accounts WHERE name LIKE 'Scanner · Replay%'", default=0)
        name = f"Scanner · Replay R{n}"
        note = (f"Aus dem Replay-Backtest: Test-Zeitraum {best['test']['rendite']:+.1f} % ({best['besser_im_test']:+.1f} %-Punkte "
                f"besser als das Hauptkonto). Änderungen: " + ", ".join(f"{params.LABELS.get(k, k)} {a}→{b}" for k, (a, b) in best["aenderungen"].items()))
        self.s.x("""INSERT INTO accounts(name,strategy,kind,params,start_balance,cash,created,active,parent,note,generation)
                    VALUES(?,?,'test',?,?,?,?,1,?,?,?)""",
                 (name, "scanner", json.dumps(best["params"]), config.START_BALANCE, config.START_BALANCE, now, main["id"], note[:400], n))
        self.s.decide("learning", "MUTATE", f"neues Testkonto {name} – {note}"[:480], main["id"])
        return name
