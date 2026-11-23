"""Schatten-Auswertung: Was ist aus den Coins geworden, die der Bot NICHT gekauft hat?

Jede Ablehnung des Hauptkontos wird mit Regel, Kurs und Liquidität gespeichert (je Coin und Regel einmal in 2 h).
Danach verfolgt der Bot den Kurs SHADOW_H Stunden weiter und ordnet ein:
  Gewinner   bestes Plus ≥ +50 % (mit Trailing-Stop wäre da meist etwas hängen geblieben)
  Rug        Kurs am Ende ≤ −80 % oder Liquidität um mehr als 70 % eingebrochen
  Verlierer  nie mehr als +10 % und am Ende im Minus
  neutral    alles dazwischen
Zum Vergleich wird jeder Kauf des Hauptkontos genauso verfolgt („gekauft“).
Eine Regel, die mehr Gewinner und nicht mehr Rugs blockiert als der Durchschnitt der Käufe, ist eher zu streng.

Hinweis: Fällt ein Coin durch mehrere Regeln, zählt nur die erste – „Lockern“ dieser Regel allein hätte ihn
also nicht immer durchgelassen. Die Zahlen sind ein Hinweis, kein Beweis.
"""
import json
import time
from statistics import median

from .. import config
from .analysis import RULE_PARAM, rule_name

WIN, RUG, LOSS, NEUTRAL, UNKNOWN = "gewinner", "rug", "verlierer", "neutral", "unbekannt"


def classify(price, max_price, last_price, liq, last_liq, updated):
    if not updated or not price:
        return UNKNOWN, 0.0, 0.0
    max_gain = (max_price / price - 1) * 100
    final = (last_price / price - 1) * 100
    if final <= -80 or (liq and last_liq is not None and last_liq < liq * 0.3):
        return RUG, max_gain, final
    if max_gain >= 50:
        return WIN, max_gain, final
    if max_gain < 10 and final < 0:
        return LOSS, max_gain, final
    return NEUTRAL, max_gain, final


class Shadow:
    name = "shadow"

    def __init__(self, store):
        self.s = store
        self._seen = {}
        self.stats = {"erfasst": 0, "ausgewertet": 0}

    def record(self, strategy, mint, symbol, rule, reason, d, parts=None):
        now = time.time()
        k = (strategy, mint, rule)
        if now - self._seen.get(k, 0) < config.SHADOW_H * 3600:
            return False
        if len(self._seen) > 50000:
            self._seen = {kk: t for kk, t in self._seen.items() if now - t < config.SHADOW_H * 3600}
        self._seen[k] = now
        price = d.get("price") or 0
        if price <= 0:
            return False
        liq = d.get("liq") or (d.get("reserve") or 0) * 2
        self.s.x("""INSERT INTO shadow(mint,symbol,strategy,rule,reason,ts,price,liq,max_price,min_price,last_price,last_liq,parts)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (mint, symbol, strategy, rule, reason[:200], now, price, liq, price, price, price, liq,
                  json.dumps({k: v["wert"] for k, v in parts.items()}) if parts else None))
        self.stats["erfasst"] += 1
        return True

    def update(self, updated):
        """Nach jedem Kurs-Update: Hoch, Tief und letzten Stand der verfolgten Coins nachziehen."""
        if not updated:
            return
        now = time.time()
        rows = [(d["price"], d["price"], d["price"], d.get("liq") or d.get("reserve", 0) * 2, now, mint)
                for mint, d in updated.items() if d.get("price")]
        self.s.many("""UPDATE shadow SET max_price=MAX(max_price,?), min_price=MIN(min_price,?), last_price=?, last_liq=?, updated=?
                       WHERE mint=? AND done=0""", rows)

    def finalize(self):
        cut = time.time() - config.SHADOW_H * 3600
        rows = self.s.q("SELECT * FROM shadow WHERE done=0 AND ts<? LIMIT 2000", (cut,))
        out = []
        for r in rows:
            oc, mg, fin = classify(r["price"], r["max_price"], r["last_price"], r["liq"], r["last_liq"], r["updated"])
            out.append((round(mg, 1), round(fin, 1), oc, r["id"]))
        self.s.many("UPDATE shadow SET done=1, max_gain=?, final=?, outcome=? WHERE id=?", out)
        self.stats["ausgewertet"] += len(out)
        return len(out)

    # ------------------------------------------------------------ Auswertung
    def report(self, days=7):
        since = time.time() - days * 86400
        raw = self.s.q("""SELECT strategy, rule, outcome, max_gain, final FROM shadow
                          WHERE done=1 AND outcome!='unbekannt' AND ts>?""", (since,))
        groups = {}
        for r in raw:
            g = groups.setdefault((r["strategy"], r["rule"]), {"n": 0, "win": 0, "rug": 0, "loss": 0, "mg": [], "fin": []})
            g["n"] += 1
            g["win"] += r["outcome"] == WIN
            g["rug"] += r["outcome"] == RUG
            g["loss"] += r["outcome"] == LOSS
            g["mg"].append(r["max_gain"] or 0)
            g["fin"].append(r["final"] or 0)
        rows = [{"strategy": k[0], "rule": k[1], **v} for k, v in groups.items()]
        pending = {(r["strategy"], r["rule"]): r["n"] for r in self.s.q(
            "SELECT strategy, rule, COUNT(*) n FROM shadow WHERE done=0 GROUP BY strategy, rule")}
        out = {}
        for strat in sorted({r["strategy"] for r in rows} | {k[0] for k in pending}):
            mine = [r for r in rows if r["strategy"] == strat]
            base = next((r for r in mine if r["rule"] == "gekauft"), None)
            bw = base["win"] / base["n"] * 100 if base and base["n"] else None
            br = base["rug"] / base["n"] * 100 if base and base["n"] else None
            items = []
            for r in mine:
                wr, rr = r["win"] / r["n"] * 100, r["rug"] / r["n"] * 100
                items.append({
                    "rule": r["rule"], "name": rule_name(r["rule"]), "n": r["n"], "offen": pending.get((strat, r["rule"]), 0),
                    "gewinner_pct": round(wr, 1), "rug_pct": round(rr, 1), "verlierer_pct": round(r["loss"] / r["n"] * 100, 1),
                    # Median statt Durchschnitt: einzelne Ausreißer (Datenfehler, ×1000-Coins) verzerren sonst alles
                    "avg_bestes": round(median(r["mg"]), 1), "avg_ende": round(median(r["fin"]), 1),
                    "einschaetzung": self._verdict(r["rule"], r["n"], wr, rr, bw, br),
                    "regel": RULE_PARAM.get(r["rule"], (None,))[0],
                })
            for (s_, rule), n in pending.items():
                if s_ == strat and not any(i["rule"] == rule for i in items):
                    items.append({"rule": rule, "name": rule_name(rule), "n": 0, "offen": n, "einschaetzung": "läuft noch",
                                  "regel": RULE_PARAM.get(rule, (None,))[0]})
            items.sort(key=lambda i: (i["rule"] != "gekauft", -(i["n"] or 0)))
            out[strat] = {"basis_gewinner_pct": round(bw, 1) if bw is not None else None,
                          "basis_rug_pct": round(br, 1) if br is not None else None, "regeln": items}
        return out

    @staticmethod
    def _verdict(rule, n, wr, rr, bw, br):
        if rule == "gekauft":
            return "Vergleichswert"
        if n < config.SHADOW_MIN_N:
            return f"zu wenig Daten (ab {config.SHADOW_MIN_N})"
        if bw is None:
            return "noch kein Vergleich (keine ausgewerteten Käufe)"
        if wr >= bw and rr <= br + 5:
            return "eher zu streng"
        if rr >= br + 10 or wr <= bw / 2:
            return "schützt gut"
        return "unklar"

    def suggestion(self, strategy):
        """Regel, die am deutlichsten zu streng wirkt -> (Regel im Konto, Richtung) oder None."""
        rep = self.report().get(strategy) or {}
        bw = rep.get("basis_gewinner_pct")
        cands = [i for i in rep.get("regeln", []) if i.get("einschaetzung") == "eher zu streng" and i["rule"] in RULE_PARAM]
        if not cands or bw is None:
            return None
        best = max(cands, key=lambda i: (i["gewinner_pct"] - bw) * min(1.0, i["n"] / 100))
        param, direction = RULE_PARAM[best["rule"]]
        return {"rule": best["rule"], "param": param, "direction": direction, "info": best}

    def samples(self, strategy="scanner", days=14):
        """Für das Gewichts-Lernen: (Bestandteile, Gewinner ja/nein) aus abgelehnten Coins mit Punktzahl."""
        rows = self.s.q("""SELECT parts, outcome FROM shadow WHERE strategy=? AND done=1 AND parts IS NOT NULL
                           AND outcome IN ('gewinner','rug','verlierer') AND ts>?""", (strategy, time.time() - days * 86400))
        return [(json.loads(r["parts"]), 1 if r["outcome"] == WIN else 0) for r in rows]
