"""Regelsätze der Strategien. Jedes Konto (Haupt- und Testkonto) hat einen eigenen Satz.

Der Lern-Agent verändert nur Testkonten selbst. Ein Hauptkonto ändert sich erst, wenn Luis einen Vorschlag freigibt.
"""
import random

# Erklärung jeder Regel – wird im Dashboard angezeigt
LABELS = {
    "min_score": "Mindest-Punktzahl des Analysten",
    "size_min_pct": "Einsatz bei Mindest-Punktzahl (% vom Konto)",
    "size_max_pct": "Einsatz bei 100 Punkten (% vom Konto)",
    "max_positions": "Max. offene Positionen",
    "max_exposure_pct": "Max. Anteil im Markt (%)",
    "max_impact_pct": "Max. Kursbewegung durch eigenen Kauf (%)",
    "stop_pct": "Stop-Loss (%)",
    "trail_start_pct": "Trailing-Stop startet ab Gewinn (%)",
    "trail_pct": "Trailing-Abstand vom Höchststand (%)",
    "tp1_pct": "Teilverkauf ab Gewinn (%)",
    "tp1_sell_pct": "Teilverkauf: Anteil (%)",
    "max_hold_min": "Max. Haltedauer (min)",
    "min_liq": "Mindest-Liquidität ($)",
    "min_mcap": "Mindest-Marktkapitalisierung ($)",
    "max_mcap": "Max. Marktkapitalisierung ($)",
    "min_age_min": "Mindestalter des Pools (min)",
    "max_age_h": "Höchstalter des Pools (h)",
    "min_vol_m5": "Mindest-Volumen 5 min ($)",
    "min_buys_m5": "Mindest-Käufe 5 min",
    "min_buy_ratio": "Mindest-Verhältnis Käufe/Verkäufe",
    "min_m5_change": "Mindest-Anstieg 5 min (%)",
    "max_m5_change": "Kein Hinterherrennen: max. Anstieg 5 min (%)",
    "max_top10_pct": "Max. Anteil der 10 größten Holder (%)",
    "min_holders": "Mindestzahl Holder",
    "reentry_min": "Sperre nach Verkauf für denselben Coin (min)",
    "max_chase_pct": "Copy: max. Aufpreis zur Wallet (%)",
    "min_wallet_score": "Copy: Mindest-Wallet-Score",
    "follow_sells": "Copy: mitverkaufen, wenn die Wallet verkauft",
    "max_creator_tokens": "Max. Coins des Erstellers (Serien-Launcher)",
    "weights": "Gewichte des Analysten",
    "time_stop_min": "Zeit-Stop nach (min, 0 = aus)",
    "time_stop_gain_pct": "Zeit-Stop: nötiges Plus bis dahin (%)",
    "flow_exit": "Ausstieg, wenn der Kaufdruck kippt (1 = an)",
    "flow_ratio": "Kaufdruck gekippt ab Verkäufe/Käufe",
    "vol_stop": "Stop nach Schwankung (1 = an)",
    "vol_mult": "Stop nach Schwankung: Faktor",
    "regime_cold_mult": "Einsatz-Faktor bei kaltem Markt",
    "regime_pause_heat": "Pause unter Markt-Hitze (0 = nie)",
    "min_wallets": "Copy: mind. so viele Smart Wallets kaufen",
    "min_h1_change": "Mindest-Anstieg 1 h (%)",
    "exclude_curve": "Nur migrierte Coins, keine Bonding-Kurve (1 = an)",
    "rug_watch": "Rug-Wächter: bei Rug-Vorzeichen sofort verkaufen (1 = an)",
    "curve_size_mult": "Einsatz-Faktor auf der Bonding-Kurve",
    "smart_min": "Mind. so viele Smart Wallets halten den Coin (0 = egal)",
    "smart_bonus": "Bonus-Punkte je Smart Wallet im Coin",
}

SCANNER_DEFAULT = {
    "min_score": 60, "size_min_pct": 4, "size_max_pct": 10, "max_positions": 6, "max_exposure_pct": 60,
    "max_impact_pct": 2.5, "stop_pct": 15, "trail_start_pct": 25, "trail_pct": 15, "tp1_pct": 100,
    "tp1_sell_pct": 50, "max_hold_min": 120, "min_liq": 8000, "min_mcap": 10000, "max_mcap": 5_000_000,
    "min_age_min": 3, "max_age_h": 12, "min_vol_m5": 3000, "min_buys_m5": 20, "min_buy_ratio": 1.2,
    "min_m5_change": 3, "max_m5_change": 120, "max_top10_pct": 30, "min_holders": 50, "reentry_min": 60,
    "max_creator_tokens": 5, "time_stop_min": 0, "time_stop_gain_pct": 5, "flow_exit": 0, "flow_ratio": 1.3,
    "vol_stop": 0, "vol_mult": 0.6, "regime_cold_mult": 1.0, "regime_pause_heat": 0, "min_h1_change": -100, "exclude_curve": 0,
    "rug_watch": 0, "curve_size_mult": 1.0, "smart_min": 0, "smart_bonus": 0,
}
# v1.4: Schutz-Bausteine, die alle Testkonten bekommen (Hauptkonto erst nach Freigabe)
SCHUTZ_V14 = {"rug_watch": 1, "curve_size_mult": 0.5}

# v1.3: Bausteine der neuen Strategie-Familien (nach 22 h Live-Daten: Hinterherrennen nach großen Anstiegen verliert)
_RUECKSETZER = {"min_h1_change": 30, "min_m5_change": -20, "max_m5_change": 0, "min_buy_ratio": 0.8, "min_buys_m5": 10,
                "min_vol_m5": 1500, "min_score": 40}
_GROSS = {"min_liq": 30000, "min_age_min": 60, "min_mcap": 100000, "max_m5_change": 40, "min_score": 45}

COPY_DEFAULT = dict(SCANNER_DEFAULT, **{
    "min_score": 45, "min_vol_m5": 0, "min_buys_m5": 0, "min_buy_ratio": 0, "min_m5_change": -100,
    "max_m5_change": 300, "min_age_min": 0, "max_age_h": 72, "max_chase_pct": 15, "min_wallet_score": 0.45,
    "follow_sells": 1, "min_wallets": 1,
})

# Startvarianten der Testkonten. „TikTok“ = die Regeln aus den Screenshots (Stop −6 %, Trailing ab +12 %, 8 % Einsatz).
VARIANTS = {
    "scanner": {
        "Vorsichtig": {"min_score": 70, "stop_pct": 10, "trail_start_pct": 20, "trail_pct": 10, "max_positions": 4},
        "Kein Hinterherrennen": {"min_m5_change": 0, "max_m5_change": 25, "min_score": 45},
        "Rücksetzer": dict(_RUECKSETZER),
        "Nur migriert": {"exclude_curve": 1, "max_m5_change": 40, "min_score": 45},
        "Groß & alt": dict(_GROSS),
        "Migriert + Rücksetzer": dict(_RUECKSETZER, exclude_curve=1),
        "Groß + Rücksetzer": dict(_GROSS, **_RUECKSETZER),
        "Smart-Money bestätigt": {"min_m5_change": 0, "max_m5_change": 25, "min_score": 40, "smart_min": 1, "smart_bonus": 10},
        "Rücksetzer + Smart-Money": dict(_RUECKSETZER, smart_min=1, smart_bonus=10),
    },
    "copy": {
        "Streng": {"max_chase_pct": 8, "min_wallet_score": 0.55, "stop_pct": 12},
        "Locker": {"max_chase_pct": 30, "min_wallet_score": 0.40, "stop_pct": 25, "trail_pct": 25},
        "TikTok": {"stop_pct": 6, "trail_start_pct": 12, "trail_pct": 8, "size_min_pct": 8, "size_max_pct": 8},
        "Cluster": {"min_wallets": 2},
        "Schneller Ausstieg": {"time_stop_min": 10, "flow_exit": 1},
    },
}

# Was der Lern-Agent bei neuen Testvarianten verändern darf, mit Grenzen und Schrittweite
MUTABLE = {
    "min_score": (35, 90, 5), "stop_pct": (4, 40, 2), "trail_start_pct": (8, 80, 4), "trail_pct": (5, 40, 2),
    "tp1_pct": (25, 300, 25), "tp1_sell_pct": (20, 80, 10), "max_hold_min": (15, 360, 15),
    "min_buy_ratio": (0.8, 3.0, 0.1), "max_m5_change": (20, 300, 20), "min_vol_m5": (500, 20000, 500),
    "max_chase_pct": (3, 50, 3), "min_wallet_score": (0.35, 0.8, 0.05),
    "min_liq": (2000, 50000, 1000), "min_mcap": (3000, 100000, 2000), "max_mcap": (200000, 50_000_000, 500000),
    "min_age_min": (0, 30, 1), "max_age_h": (1, 72, 1), "min_buys_m5": (0, 150, 5), "min_m5_change": (-30, 30, 2),
    "max_top10_pct": (15, 60, 5), "min_holders": (10, 400, 10), "max_creator_tokens": (1, 30, 1),
    "time_stop_min": (0, 60, 5), "time_stop_gain_pct": (0, 30, 5), "flow_exit": (0, 1, 1), "flow_ratio": (1.1, 3.0, 0.1),
    "vol_stop": (0, 1, 1), "vol_mult": (0.3, 1.5, 0.1), "regime_cold_mult": (0.2, 1.0, 0.1),
    "min_h1_change": (-100, 200, 10), "exclude_curve": (0, 1, 1),
    "curve_size_mult": (0.2, 1.0, 0.1), "smart_bonus": (0, 30, 5),
}
# Testkonten, die in v1.3 abgeschaltet werden: alles Abwandlungen von „nach großem Anstieg kaufen“ (22 h: −55 bis −74 %)
RETIRED_V13 = ["Locker", "Schnell", "TikTok", "Zeit-Stop", "Kaufdruck-Ausstieg", "Schwankungs-Stop", "Marktlage"]
# Diese Regeln verändert der Lern-Agent nur gezielt (nach der Schatten-Auswertung), nicht zufällig
DIRECTED_ONLY = {"min_liq", "min_mcap", "max_mcap", "min_age_min", "max_age_h", "min_buys_m5", "min_m5_change",
                 "max_top10_pct", "min_holders", "max_creator_tokens"}


for _strat in VARIANTS.values():
    for _name, _changes in _strat.items():
        for _k, _v in SCHUTZ_V14.items():
            _changes.setdefault(_k, _v)


def defaults(strategy):
    return dict(COPY_DEFAULT if strategy == "copy" else SCANNER_DEFAULT)


def mutate(params, strategy, rng=None, n_changes=2):
    """Neue Testvariante: 1–n Regeln einen bis zwei Schritte verschieben (innerhalb der Grenzen)."""
    rng = rng or random.Random()
    out = dict(params)
    keys = [k for k in MUTABLE if k in out and k not in DIRECTED_ONLY
            and (strategy == "copy" or k not in ("max_chase_pct", "min_wallet_score"))]
    changed = {}
    for k in rng.sample(keys, k=min(len(keys), rng.randint(1, n_changes))):
        lo, hi, step = MUTABLE[k]
        v = out[k] + step * rng.choice([-2, -1, 1, 2])
        v = max(lo, min(hi, v))
        v = round(v, 2) if isinstance(step, float) else int(round(v))
        if v != out[k]:
            changed[k] = (out[k], v)
            out[k] = v
    if out["trail_pct"] >= out["trail_start_pct"]:
        out["trail_pct"] = max(5, out["trail_start_pct"] - 2)
    return out, changed


def loosen(params, strategy, key, direction, steps=1):
    """Gezielte Änderung: eine Regel in Richtung „lockerer“ verschieben (Vorschlag der Schatten-Auswertung)."""
    out = dict(defaults(strategy), **params)
    lo, hi, step = MUTABLE[key]
    old = out[key]
    v = max(lo, min(hi, old + direction * step * steps))
    v = round(v, 2) if isinstance(step, float) else int(round(v))
    out[key] = v
    return out, ({key: (old, v)} if v != old else {})


def diff(a, b):
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
