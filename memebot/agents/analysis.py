"""Rug-Check-Agent und Analyst.

Rug-Check: harte Ausschlussregeln (wer durchfällt, wird nie gekauft).
Analyst:   Punktzahl 0–100 aus Momentum, Kaufdruck, Volumen-Beschleunigung, Aktivität, Liquidität, Sicherheit und Chart.
           Die Gewichte stehen in den Regeln jedes Kontos – der Lern-Agent kann neue Gewichte in einem Testkonto ausprobieren.

Jede Regel hat einen kurzen Schlüssel (z. B. „liquiditaet“, „rug:top10“). Die Schatten-Auswertung zählt damit,
was aus den abgelehnten Coins geworden ist.
"""
import json
import time

from .. import config, market
from ..sources import SourceError, available

# Regel-Schlüssel -> (Regel im Konto, Richtung zum Lockern), für die Schatten-Auswertung und den Lern-Agenten
RULE_PARAM = {
    "zu_jung": ("min_age_min", -1), "zu_alt": ("max_age_h", 1), "liquiditaet": ("min_liq", -1),
    "mcap_klein": ("min_mcap", -1), "mcap_gross": ("max_mcap", 1), "volumen": ("min_vol_m5", -1),
    "kaeufe": ("min_buys_m5", -1), "kaufdruck": ("min_buy_ratio", -1), "anstieg_klein": ("min_m5_change", -1),
    "anstieg_gross": ("max_m5_change", 1), "rug:top10": ("max_top10_pct", 1), "rug:holder": ("min_holders", -1),
    "rug:serie": ("max_creator_tokens", 1), "punktzahl": ("min_score", -1), "aufpreis": ("max_chase_pct", 1),
    "anstieg_1h": ("min_h1_change", -1), "kurve": ("exclude_curve", -1), "smart": ("smart_min", -1),
}
RULE_NAMES = {
    "zu_jung": "Pool zu jung", "zu_alt": "Pool zu alt", "liquiditaet": "Liquidität zu klein",
    "mcap_klein": "Marktkap. zu klein", "mcap_gross": "Marktkap. zu groß", "volumen": "Volumen 5 min zu klein",
    "kaeufe": "zu wenige Käufe", "kaufdruck": "Kaufdruck zu schwach", "anstieg_klein": "Anstieg zu klein",
    "anstieg_gross": "schon zu weit gestiegen", "punktzahl": "Punktzahl zu niedrig", "gekauft": "gekauft (Vergleich)",
    "rug:mint": "Mint-Funktion an", "rug:freeze": "Freeze-Funktion an", "rug:fee": "Transfergebühr",
    "rug:rugged": "schon gerugged", "rug:top10": "Top 10 zu groß", "rug:holder": "zu wenige Holder",
    "rug:ersteller": "Ersteller hält zu viel", "rug:lp": "Liquidität nicht gesperrt", "rug:insider": "Insider-Netzwerk",
    "rug:serie": "Serien-Launcher", "aufpreis": "zu teuer gegenüber der Wallet", "rug:mehrere": "mehrere Rug-Regeln",
    "anstieg_1h": "Anstieg 1 h zu klein", "kurve": "noch auf der Bonding-Kurve",
    "smart": "keine Smart Wallet im Coin", "rugwaechter": "Rug-Wächter-Verkauf (danach)",
}
CURVE_DEX = {"pumpfun", "pump-fun", "meteoradbc", "launchlab", "raydium-launchlab", "moonshot", "boop"}


def is_curve_dex(dex):
    """Coin handelt noch auf einer Bonding-Kurve (Pump.fun, Meteora DBC, LaunchLab …), also vor der Migration."""
    d = (dex or "").lower()
    return d in CURVE_DEX or "dbc" in d or "launchlab" in d


def rule_name(key):
    if key.startswith("rug:gefahr:"):
        return "Gefahr-Warnung: " + key.split(":", 2)[2]
    return RULE_NAMES.get(key, key)


# ---------------------------------------------------------------- Vorfilter (billig, ohne Abruf)
def check_pre(d, p):
    """Lohnt sich ein genauerer Blick? -> (ja/nein, Regel-Schlüssel, Grund)"""
    liq = d["liq"] or d["reserve"] * 2
    if p.get("exclude_curve") and is_curve_dex(d.get("dex")):
        return False, "kurve", f"noch auf der Bonding-Kurve ({d.get('dex')})"
    if d["age_min"] < p["min_age_min"]:
        return False, "zu_jung", f"zu jung ({d['age_min']:.0f} min)"
    if d["age_min"] > p["max_age_h"] * 60:
        return False, "zu_alt", f"zu alt ({d['age_min'] / 60:.0f} h)"
    if liq < p["min_liq"]:
        return False, "liquiditaet", f"Liquidität {liq:,.0f} $ < {p['min_liq']:,.0f} $"
    if d["mcap"] < p["min_mcap"]:
        return False, "mcap_klein", f"Marktkapitalisierung {d['mcap']:,.0f} $ zu klein"
    if d["mcap"] > p["max_mcap"]:
        return False, "mcap_gross", f"Marktkapitalisierung {d['mcap']:,.0f} $ zu groß"
    if d["vol_m5"] < p["min_vol_m5"]:
        return False, "volumen", f"Volumen 5 min {d['vol_m5']:,.0f} $ zu klein"
    if d["buys_m5"] < p["min_buys_m5"]:
        return False, "kaeufe", f"nur {d['buys_m5']} Käufe in 5 min"
    ratio = d["buys_m5"] / max(1, d["sells_m5"])
    if ratio < p["min_buy_ratio"]:
        return False, "kaufdruck", f"Kaufdruck {ratio:.2f} zu schwach"
    if d["ch_m5"] < p["min_m5_change"]:
        return False, "anstieg_klein", f"Anstieg 5 min {d['ch_m5']:+.0f} % zu klein"
    if d["ch_m5"] > p["max_m5_change"]:
        return False, "anstieg_gross", f"schon +{d['ch_m5']:.0f} % in 5 min – nicht hinterherrennen"
    if d.get("ch_h1", 0) < p.get("min_h1_change", -100):
        return False, "anstieg_1h", f"Anstieg 1 h {d.get('ch_h1', 0):+.0f} % zu klein"
    return True, "", ""


def prefilter(d, p):
    ok, _, why = check_pre(d, p)
    return ok, why


# ---------------------------------------------------------------- Rug-Check
class RugGuard:
    name = "rugcheck"

    def __init__(self, store):
        self.s = store
        self.stats = {"geprueft": 0, "blockiert": 0, "fehler": ""}

    def report(self, mint, pool):
        """Bericht aus dem Zwischenspeicher oder frisch von RugCheck. None = gerade kein Abruf möglich."""
        row = self.s.one("SELECT rug, rug_at FROM tokens WHERE mint=?", (mint,))
        r = None
        if row and row["rug"] and time.time() - (row["rug_at"] or 0) < config.RUGCHECK_CACHE_MIN * 60:
            r = json.loads(row["rug"])
        if r is None:
            if available("rugcheck") <= 0:
                return None
            try:
                r = market.rugcheck(mint, (pool,))
            except SourceError as e:
                self.stats["fehler"] = str(e)
                return None
            self.stats["geprueft"] += 1
            now = time.time()
            first = self.s.one("SELECT holders_first, holders_first_at FROM tokens WHERE mint=?", (mint,)) or {}
            if not first.get("holders_first"):
                self.s.x("UPDATE tokens SET holders_first=?, holders_first_at=? WHERE mint=?", (r["holders"], now, mint))
            elif now - first["holders_first_at"] >= 300:
                r["holder_growth_h"] = round((r["holders"] - first["holders_first"]) / ((now - first["holders_first_at"]) / 3600), 1)
            self.s.x("UPDATE tokens SET rug=?, rug_at=? WHERE mint=?", (json.dumps(r), now, mint))
        # Wie viele Coins hat der Ersteller in den letzten 24 h gestartet? (eigener Live-Stream von Pump.fun)
        if r.get("creator"):
            n = self.s.val("SELECT COUNT(*) FROM launches WHERE creator=? AND ts>?", (r["creator"], time.time() - 86400), 0)
            r["creator_launches"] = max(r.get("creator_tokens", 0), n)
        return r

    @staticmethod
    def checks(r, p):
        """Liste von (Name, bestanden, Wert, Schlüssel) – alle müssen bestanden sein."""
        danger = [x["name"] for x in r["risks"] if (x.get("level") or "") == "danger"]
        serie = r.get("creator_launches", r.get("creator_tokens", 0)) or 0
        max_serie = p.get("max_creator_tokens", 5)
        c = [
            ("Mint-Funktion aus", not r["mint_auth"], "aus" if not r["mint_auth"] else "AN", "rug:mint"),
            ("Freeze-Funktion aus", not r["freeze_auth"], "aus" if not r["freeze_auth"] else "AN", "rug:freeze"),
            ("keine Transfergebühr", r["transfer_fee"] <= 0, f"{r['transfer_fee']:.1f} %", "rug:fee"),
            ("nicht gerugged", not r["rugged"], "ok" if not r["rugged"] else "GERUGGED", "rug:rugged"),
            (f"Top 10 ≤ {p['max_top10_pct']} %", r["top10_pct"] <= p["max_top10_pct"], f"{r['top10_pct']:.0f} %", "rug:top10"),
            (f"≥ {p['min_holders']} Holder", r["holders"] >= p["min_holders"], str(r["holders"]), "rug:holder"),
            ("Ersteller ≤ 5 %", r["creator_pct"] <= 5, f"{r['creator_pct']:.1f} %", "rug:ersteller"),
            ("Liquidität gesperrt ≥ 90 %", r["lp_locked_pct"] >= 90, f"{r['lp_locked_pct']:.0f} %", "rug:lp"),
            ("keine Insider-Netzwerke", r["insider_networks"] == 0, str(r["insider_networks"]), "rug:insider"),
            (f"kein Serien-Launcher (≤ {max_serie} Coins)", serie <= max_serie, str(serie), "rug:serie"),
            ("keine Gefahr-Warnung", not danger, ", ".join(danger)[:80] or "keine",
             "rug:gefahr:" + (danger[0] if danger else "")),
        ]
        return c

    def check(self, mint, pool, p):
        """-> (Status, Prüfliste, Bericht)   Status: 'ok' | 'block' | 'warten'"""
        r = self.report(mint, pool)
        if r is None:
            return "warten", [], None
        c = self.checks(r, p)
        if all(x[1] for x in c):
            return "ok", c, r
        self.stats["blockiert"] += 1
        return "block", c, r


# ---------------------------------------------------------------- Analyst
def _lin(x, lo, hi):
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


WEIGHTS = {"momentum": 20, "kaufdruck": 20, "beschleunigung": 15, "aktivitaet": 10, "liquiditaet": 10,
           "sicherheit": 15, "chart": 10,
           # Beobachtungs-Merkmale: zählen erst, wenn der Lern-Agent eine Wirkung findet und ihnen ein Gewicht gibt
           "kein_boost": 0, "holder_wachstum": 0, "hat_socials": 0, "kein_sniper": 0}


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


def components(d, hist, rug):
    """Die 7 Bestandteile, jeweils 0–1 mit kurzer Erklärung."""
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
    parts["chart"] = chart_score(hist, d["price"])
    boosts = d.get("boosts") or 0
    parts["kein_boost"] = (0.0 if boosts else 1.0, f"{boosts} DexScreener-Boosts" if boosts else "kein bezahlter Boost")
    g = (rug or {}).get("holder_growth_h")
    parts["holder_wachstum"] = (_lin(g, 0, 300), f"+{g:.0f} Holder/h") if g is not None else (0.5, "noch kein Verlauf")
    sn = d.get("sniper")   # {"kaeufer": Wallets in den ersten 10 s, "usd": …} oder None, wenn der Start nicht erfasst ist
    parts["kein_sniper"] = ((1 - _lin(sn["kaeufer"], 1, 8)), f"{sn['kaeufer']} Wallets kauften in den ersten 10 s") if sn \
        else (0.5, "Start nicht erfasst")
    soc = int(d.get("socials") or 0)
    parts["hat_socials"] = (1.0 if soc else 0.0, f"{soc} Links (X, Telegram, Website)" if soc else "keine Social-Links")
    return parts


def total(parts, weights=None):
    """Punktzahl 0–100 aus den Bestandteilen. Gewichte werden auf Summe 100 gebracht."""
    w = dict(WEIGHTS, **(weights or {}))
    s = sum(w.get(k, 0) for k in parts) or 1
    pts = {k: 100 * w.get(k, 0) / s * v for k, (v, _) in parts.items()}
    detail = {k: {"wert": round(v, 2), "punkte": round(pts[k], 1), "text": t, "gewicht": round(100 * w.get(k, 0) / s, 1)}
              for k, (v, t) in parts.items()}
    return round(sum(pts.values()), 1), detail


def score(d, hist, rug, weights=None):
    """-> (Punktzahl 0–100, Bestandteile)"""
    return total(components(d, hist, rug), weights)
