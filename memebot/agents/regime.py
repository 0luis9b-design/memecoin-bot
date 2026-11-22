"""Marktlage: Wie „heiß“ ist der Memecoin-Markt gerade? (0 = tot, 100 = Hype)

Alle 5 min aus Daten, die der Bot ohnehin hat:
  * neue Pump.fun-Coins pro Minute (Launch-Feed)
  * Anteil der beobachteten Coins, die in der letzten Stunde gestiegen sind
  * mittlere 1-h-Veränderung der heißen Coins
  * Zahl der heißen Coins (viel Volumen)
  * SOL-Kurs der letzten Stunde
Die Hitze wird gespeichert (Verlauf im Dashboard, Replay-Backtest). Konten mit „Marktlage“-Regeln setzen bei kaltem
Markt kleiner ein oder pausieren – das Hauptkonto erst, wenn ein Testkonto zeigt, dass es hilft.
"""
import json
import statistics
import time
from collections import deque

from .. import market


def _lin(x, lo, hi):
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) if hi != lo else 0.0


class Regime:
    name = "regime"

    def __init__(self, store):
        self.s = store
        self.heat = None
        self.parts = {}
        self._sol = deque(maxlen=40)   # (Zeit, Kurs) – alle 5 min ein Punkt
        row = self.s.one("SELECT heat, data FROM regime ORDER BY ts DESC LIMIT 1")
        if row:
            self.heat, self.parts = row["heat"], json.loads(row["data"] or "{}")

    def update(self):
        now = time.time()
        launches = self.s.val("SELECT COUNT(*) FROM launches WHERE ts>?", (now - 1800,), 0) / 30
        rows = self.s.q("SELECT metrics, hot FROM tokens WHERE status='watch' AND updated>?", (now - 600,))
        ch = []
        hot_ch = []
        for r in rows:
            m = json.loads(r["metrics"] or "{}")
            if "ch_h1" in m:
                ch.append(m["ch_h1"])
                if r["hot"]:
                    hot_ch.append(m["ch_h1"])
        sol = market.sol_usd()
        self._sol.append((now, sol))
        old = next((p for t, p in self._sol if now - t >= 3300), None)
        sol_ch = (sol / old - 1) * 100 if old else 0.0
        parts = {
            "neue_coins_pro_min": (round(launches, 1), _lin(launches, 2, 15), 0.2),
            "anteil_im_plus": (round(sum(1 for x in ch if x > 0) / len(ch) * 100, 1) if ch else None,
                               _lin(sum(1 for x in ch if x > 0) / len(ch), 0.25, 0.6) if ch else 0.5, 0.25),
            "heisse_coins_1h": (round(statistics.median(hot_ch), 1) if hot_ch else None,
                                _lin(statistics.median(hot_ch), -10, 40) if hot_ch else 0.3, 0.25),
            "anzahl_heiss": (len(hot_ch), _lin(len(hot_ch), 5, 40), 0.2),
            "sol_1h": (round(sol_ch, 2), _lin(sol_ch, -3, 3), 0.1),
        }
        heat = round(100 * sum(v * w for _, v, w in parts.values()) / sum(w for _, _, w in parts.values()), 1)
        self.heat = heat
        self.parts = {k: {"wert": v, "anteil": round(s, 2)} for k, (v, s, _) in parts.items()}
        self.s.x("INSERT OR REPLACE INTO regime(ts, heat, data) VALUES(?,?,?)", (now, heat, json.dumps(self.parts)))
        return heat

    @staticmethod
    def label(heat):
        if heat is None:
            return "unbekannt"
        return "kalt" if heat < 30 else "normal" if heat < 60 else "heiß"
