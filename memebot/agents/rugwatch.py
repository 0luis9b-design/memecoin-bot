"""Rug-Wächter: offene Positionen laufend auf Rug-Vorzeichen prüfen.

Alle RUGWATCH_SEC Sekunden holt der Bot für jeden Coin mit offener Position einen frischen RugCheck-Bericht und
vergleicht ihn mit dem vorherigen. Verkauft wird sofort, wenn
  * der Ersteller mindestens die Hälfte seiner Coins abgestoßen hat,
  * ein großer Holder (≥ 3 %) mindestens die Hälfte verkauft hat,
  * die Zahl der Holder um mehr als 10 % fällt,
  * die Liquidität nicht mehr gesperrt ist,
  * RugCheck eine neue Gefahr-Warnung meldet oder den Coin als gerugged markiert.
Das gilt für Konten mit der Regel rug_watch = 1. Was danach mit dem Kurs passiert ist, zeigt die Schatten-Auswertung
(Zeile „Rug-Wächter-Verkauf“): Viele Rugs dort = der Wächter hat Geld gerettet.
"""
import json
import time

from .. import config, market
from ..sources import SourceError, available


def compare(old, new):
    """Was hat sich zwischen zwei Berichten verschlechtert? -> Liste von Gründen"""
    if not old or not new:
        return []
    why = []
    if new.get("rugged") and not old.get("rugged"):
        why.append("RugCheck meldet: gerugged")
    old_danger = {r["name"] for r in old.get("risks", []) if r.get("level") == "danger"}
    fresh = [r["name"] for r in new.get("risks", []) if r.get("level") == "danger" and r["name"] not in old_danger]
    if fresh:
        why.append("neue Gefahr-Warnung: " + ", ".join(fresh)[:80])
    oc, nc = old.get("creator_pct", 0), new.get("creator_pct", 0)
    if oc >= 1 and nc < oc * 0.5:
        why.append(f"Ersteller verkauft ({oc:.1f} % → {nc:.1f} %)")
    if old.get("top_holders") and new.get("top_holders") is not None:
        now = {o: p for o, p in new["top_holders"]}
        for owner, pct in old["top_holders"]:
            if pct >= 3 and now.get(owner, 0) < pct * 0.5:
                why.append(f"großer Holder {str(owner)[:4]}… verkauft ({pct:.1f} % → {now.get(owner, 0):.1f} %)")
                break
    oh, nh = old.get("holders", 0), new.get("holders", 0)
    if oh >= 50 and nh < oh * 0.9:
        why.append(f"Holder fallen ({oh} → {nh})")
    if old.get("lp_locked_pct", 100) >= 90 and new.get("lp_locked_pct", 100) < 90:
        why.append(f"Liquidität entsperrt ({new.get('lp_locked_pct', 0):.0f} % gesperrt)")
    return why


class RugWatch:
    name = "rugwatch"

    def __init__(self, store, trader, shadow):
        self.s = store
        self.t = trader
        self.shadow = shadow
        self.last = {}
        self.stats = {"geprueft": 0, "verkauft": 0, "letzte_warnung": ""}

    def check(self):
        now = time.time()
        positions = [p for p in self.t.open_positions() if p["status"] == "open"]
        by_mint = {}
        for p in positions:
            by_mint.setdefault(p["mint"], []).append(p)
        accs = {a["id"]: a for a in self.t.accounts()}
        for mint, plist in by_mint.items():
            if not any(accs.get(p["account_id"], {}).get("params", {}).get("rug_watch") for p in plist):
                continue
            if now - self.last.get(mint, 0) < config.RUGWATCH_SEC or available("rugcheck") < 3:
                continue
            self.last[mint] = now
            row = self.s.one("SELECT rug, metrics, symbol FROM tokens WHERE mint=?", (mint,))
            old = json.loads(row["rug"]) if row and row["rug"] else None
            try:
                new = market.rugcheck(mint, (plist[0]["pool"],))
            except SourceError:
                continue
            self.stats["geprueft"] += 1
            if old:   # Werte für das Holder-Wachstum aus dem alten Bericht übernehmen
                new.setdefault("holder_growth_h", old.get("holder_growth_h"))
            self.s.x("UPDATE tokens SET rug=?, rug_at=? WHERE mint=?", (json.dumps(new), now, mint))
            why = compare(old, new)
            if not why:
                continue
            d = json.loads(row["metrics"]) if row and row["metrics"] else None
            if not d or not d.get("price"):
                continue
            reason = "Rug-Wächter: " + "; ".join(why)
            self.stats["letzte_warnung"] = f"{row['symbol']}: {why[0]}"
            sold = False
            for p in plist:
                acc = accs.get(p["account_id"])
                if acc and acc["params"].get("rug_watch"):
                    self.t.exit(p, acc, 1.0, d, reason, agent="rugwatch")
                    sold = True
            if sold:
                self.stats["verkauft"] += 1
                self.shadow.record("scanner", mint, row["symbol"], "rugwaechter", reason, d)
        if len(self.last) > 5000:
            self.last = {m: t for m, t in self.last.items() if now - t < 3600}
