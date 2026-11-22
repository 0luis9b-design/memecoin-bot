"""Rug-Check-Agent und Analyst.

Rug-Check: harte Ausschlussregeln (wer durchfällt, wird nie gekauft).
Analyst:   Punktzahl 0–100 aus Momentum, Kaufdruck, Volumen-Beschleunigung, Aktivität, Liquidität, Sicherheit und Chart.
"""
import json
import time

from .. import config, market
from ..sources import SourceError, available


# ---------------------------------------------------------------- Vorfilter (billig, ohne Abruf)
def prefilter(d, p):
    """Lohnt sich ein genauerer Blick? -> (ja/nein, Grund)"""
    liq = d["liq"] or d["reserve"] * 2
    if d["age_min"] < p["min_age_min"]:
        return False, f"zu jung ({d['age_min']:.0f} min)"
    if d["age_min"] > p["max_age_h"] * 60:
        return False, f"zu alt ({d['age_min'] / 60:.0f} h)"
    if liq < p["min_liq"]:
        return False, f"Liquidität {liq:,.0f} $ < {p['min_liq']:,.0f} $"
    if not (p["min_mcap"] <= d["mcap"] <= p["max_mcap"]):
        return False, f"Marktkapitalisierung {d['mcap']:,.0f} $ außerhalb"
    if d["vol_m5"] < p["min_vol_m5"]:
        return False, f"Volumen 5 min {d['vol_m5']:,.0f} $ zu klein"
    if d["buys_m5"] < p["min_buys_m5"]:
        return False, f"nur {d['buys_m5']} Käufe in 5 min"
    ratio = d["buys_m5"] / max(1, d["sells_m5"])
    if ratio < p["min_buy_ratio"]:
        return False, f"Kaufdruck {ratio:.2f} zu schwach"
    if d["ch_m5"] < p["min_m5_change"]:
        return False, f"Anstieg 5 min {d['ch_m5']:+.0f} % zu klein"
    if d["ch_m5"] > p["max_m5_change"]:
        return False, f"schon +{d['ch_m5']:.0f} % in 5 min – nicht hinterherrennen"
    return True, ""


# ---------------------------------------------------------------- Rug-Check
class RugGuard:
    name = "rugcheck"

    def __init__(self, store):
        self.s = store
        self.stats = {"geprueft": 0, "blockiert": 0, "fehler": ""}

    def report(self, mint, pool):
        """Bericht aus dem Zwischenspeicher oder frisch von RugCheck. None = gerade kein Abruf möglich."""
        row = self.s.one("SELECT rug, rug_at FROM tokens WHERE mint=?", (mint,))
        if row and row["rug"] and time.time() - (row["rug_at"] or 0) < config.RUGCHECK_CACHE_MIN * 60:
            return json.loads(row["rug"])
        if available("rugcheck") <= 0:
            return None
        try:
            r = market.rugcheck(mint, (pool,))
        except SourceError as e:
            self.stats["fehler"] = str(e)
            return None
        self.stats["geprueft"] += 1
        self.s.x("UPDATE tokens SET rug=?, rug_at=? WHERE mint=?", (json.dumps(r), time.time(), mint))
        return r

    @staticmethod
    def checks(r, p):
        """Liste von (Name, bestanden, Wert) – alle müssen bestanden sein."""
        danger = [x["name"] for x in r["risks"] if (x.get("level") or "") == "danger"]
        c = [
            ("Mint-Funktion aus", not r["mint_auth"], "aus" if not r["mint_auth"] else "AN"),
            ("Freeze-Funktion aus", not r["freeze_auth"], "aus" if not r["freeze_auth"] else "AN"),
            ("keine Transfergebühr", r["transfer_fee"] <= 0, f"{r['transfer_fee']:.1f} %"),
            ("nicht gerugged", not r["rugged"], "ok" if not r["rugged"] else "GERUGGED"),
            (f"Top 10 ≤ {p['max_top10_pct']} %", r["top10_pct"] <= p["max_top10_pct"], f"{r['top10_pct']:.0f} %"),
            (f"≥ {p['min_holders']} Holder", r["holders"] >= p["min_holders"], str(r["holders"])),
            ("Ersteller ≤ 5 %", r["creator_pct"] <= 5, f"{r['creator_pct']:.1f} %"),
            ("Liquidität gesperrt ≥ 90 %", r["lp_locked_pct"] >= 90, f"{r['lp_locked_pct']:.0f} %"),
            ("keine Insider-Netzwerke", r["insider_networks"] == 0, str(r["insider_networks"])),
            ("keine Gefahr-Warnung", not danger, ", ".join(danger)[:80] or "keine"),
        ]
        return c

    def check(self, mint, pool, p):
        """-> (Status, Prüfliste, Bericht)   Status: 'ok' | 'block' | 'warten'"""
        r = self.report(mint, pool)
        if r is None:
            return "warten", [], None
        c = self.checks(r, p)
        if all(ok for _, ok, _ in c):
            return "ok", c, r
        self.stats["blockiert"] += 1
        return "block", c, r


# ---------------------------------------------------------------- Analyst
def _lin(x, lo, hi):
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


WEIGHTS = {"momentum": 20, "kaufdruck": 20, "beschleunigung": 15, "aktivitaet": 10, "liquiditaet": 10,
           "sicherheit": 15, "chart": 10}


def chart_score(hist, price):
    """Aus den eigenen Schnappschüssen der letzten 30 min: über dem Schnitt, höhere Tiefs, nicht weit unter dem Hoch."""
    prices = [h["price"] for h in hist if h["price"] > 0]
    if len(prices) < 3:
        return 0.5, "zu wenig Verlauf"
    avg = sum(prices) / len(prices)
    hi = max(prices)
    third = max(1, len(prices) // 3)
    low_early, low_late = min(prices[:third]), min(prices[-third:])
    s = 0.0
    s += 0.4 if price >= avg else 0.0
    s += 0.3 if low_late >= low_early else 0.0
    dd = (hi - price) / hi * 100 if hi else 0
    s += 0.3 * (1 - _lin(dd, 5, 30))
    return s, f"über Schnitt: {'ja' if price >= avg else 'nein'}, höhere Tiefs: {'ja' if low_late >= low_early else 'nein'}, {dd:.0f} % unter Hoch"


def score(d, hist, rug):
    """-> (Punktzahl 0–100, Bestandteile)"""
    parts = {}
    m5, h1 = d["ch_m5"], d["ch_h1"]
    mom = _lin(m5, 0, 30)
    if m5 > 80:  # schon sehr weit gelaufen
        mom *= max(0.3, 1 - (m5 - 80) / 150)
    mom = 0.8 * mom + 0.2 * (1.0 if h1 > 0 else 0.0)
    parts["momentum"] = (mom, f"5 min {m5:+.0f} %, 1 h {h1:+.0f} %")
    share = d["buys_m5"] / max(1, d["buys_m5"] + d["sells_m5"])
    parts["kaufdruck"] = (_lin(share, 0.5, 0.72), f"{share * 100:.0f} % Käufe")
    accel = d["vol_m5"] * 12 / max(1.0, d["vol_h1"]) if d["vol_h1"] else 1.0
    parts["beschleunigung"] = (_lin(accel, 1, 3), f"Volumen-Tempo {accel:.1f}x")
    parts["aktivitaet"] = (_lin(d["buys_m5"], 15, 120), f"{d['buys_m5']} Käufe / 5 min")
    q = d["reserve"]
    parts["liquiditaet"] = (0.6 * _lin(q, 3000, 20000) + 0.4 * _lin(q / max(1.0, d["mcap"]), 0.03, 0.15),
                            f"{q:,.0f} $ SOL-Seite")
    if rug:
        safe = 0.4 * (1 - _lin(rug["top10_pct"], 10, 30)) + 0.4 * _lin(rug["holders"], 50, 600) + \
            0.2 * (1 - _lin(rug["score_norm"], 1, 30))
        parts["sicherheit"] = (safe, f"Top 10 {rug['top10_pct']:.0f} %, {rug['holders']} Holder")
    else:
        parts["sicherheit"] = (0.3, "kein Bericht")
    cs, ctext = chart_score(hist, d["price"])
    parts["chart"] = (cs, ctext)
    total = sum(WEIGHTS[k] * v for k, (v, _) in parts.items())
    return round(total, 1), {k: {"wert": round(v, 2), "punkte": round(WEIGHTS[k] * v, 1), "text": t}
                             for k, (v, t) in parts.items()}
