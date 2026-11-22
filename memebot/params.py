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
}

SCANNER_DEFAULT = {
    "min_score": 60, "size_min_pct": 4, "size_max_pct": 10, "max_positions": 6, "max_exposure_pct": 60,
    "max_impact_pct": 2.5, "stop_pct": 15, "trail_start_pct": 25, "trail_pct": 15, "tp1_pct": 100,
    "tp1_sell_pct": 50, "max_hold_min": 120, "min_liq": 8000, "min_mcap": 10000, "max_mcap": 5_000_000,
    "min_age_min": 3, "max_age_h": 12, "min_vol_m5": 3000, "min_buys_m5": 20, "min_buy_ratio": 1.2,
    "min_m5_change": 3, "max_m5_change": 120, "max_top10_pct": 30, "min_holders": 50, "reentry_min": 60,
}

COPY_DEFAULT = dict(SCANNER_DEFAULT, **{
    "min_score": 45, "min_vol_m5": 0, "min_buys_m5": 0, "min_buy_ratio": 0, "min_m5_change": -100,
    "max_m5_change": 300, "min_age_min": 0, "max_age_h": 72, "max_chase_pct": 15, "min_wallet_score": 0.45,
    "follow_sells": 1,
})

# Startvarianten der Testkonten. „TikTok“ = die Regeln aus den Screenshots (Stop −6 %, Trailing ab +12 %, 8 % Einsatz).
VARIANTS = {
    "scanner": {
        "Vorsichtig": {"min_score": 70, "stop_pct": 10, "trail_start_pct": 20, "trail_pct": 10, "max_positions": 4},
        "Locker": {"min_score": 50, "stop_pct": 25, "trail_start_pct": 40, "trail_pct": 25, "max_hold_min": 240},
        "Schnell": {"tp1_pct": 50, "tp1_sell_pct": 60, "max_hold_min": 45, "trail_start_pct": 15, "trail_pct": 10},
        "TikTok": {"stop_pct": 6, "trail_start_pct": 12, "trail_pct": 8, "size_min_pct": 8, "size_max_pct": 8},
    },
    "copy": {
        "Streng": {"max_chase_pct": 8, "min_wallet_score": 0.55, "stop_pct": 12},
        "Locker": {"max_chase_pct": 30, "min_wallet_score": 0.40, "stop_pct": 25, "trail_pct": 25},
        "TikTok": {"stop_pct": 6, "trail_start_pct": 12, "trail_pct": 8, "size_min_pct": 8, "size_max_pct": 8},
    },
}

# Was der Lern-Agent bei neuen Testvarianten verändern darf, mit Grenzen und Schrittweite
MUTABLE = {
    "min_score": (35, 90, 5), "stop_pct": (4, 40, 2), "trail_start_pct": (8, 80, 4), "trail_pct": (5, 40, 2),
    "tp1_pct": (25, 300, 25), "tp1_sell_pct": (20, 80, 10), "max_hold_min": (15, 360, 15),
    "min_buy_ratio": (0.8, 3.0, 0.1), "max_m5_change": (20, 300, 20), "min_vol_m5": (500, 20000, 500),
    "max_chase_pct": (3, 50, 3), "min_wallet_score": (0.35, 0.8, 0.05),
}


def defaults(strategy):
    return dict(COPY_DEFAULT if strategy == "copy" else SCANNER_DEFAULT)


def mutate(params, strategy, rng=None, n_changes=2):
    """Neue Testvariante: 1–n Regeln einen bis zwei Schritte verschieben (innerhalb der Grenzen)."""
    rng = rng or random.Random()
    out = dict(params)
    keys = [k for k in MUTABLE if k in out and (strategy == "copy" or k not in ("max_chase_pct", "min_wallet_score"))]
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


def diff(a, b):
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
