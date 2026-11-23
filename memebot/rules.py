"""Gemeinsame Handelsregeln – genau dieselben im Live-Spielgeld-Depot und im Replay-Backtest.

Einsatzgröße, Stop nach Schwankung, Marktlage und alle Ausstiegsregeln stehen nur hier.
"""


def size_pct(p, score):
    """Ziel-Einsatz in % vom Konto: linear von size_min_pct (bei Mindest-Punktzahl) bis size_max_pct (bei 100)."""
    span = max(1.0, 100 - p["min_score"])
    return p["size_min_pct"] + (p["size_max_pct"] - p["size_min_pct"]) * max(0.0, min(1.0, (score - p["min_score"]) / span))


def regime_mult(p, heat):
    """Einsatz-Faktor nach Marktlage. None = gar nicht kaufen (Pause)."""
    if heat is None:
        return 1.0
    if p.get("regime_pause_heat", 0) and heat < p["regime_pause_heat"]:
        return None
    if heat < 30:
        return p.get("regime_cold_mult", 1.0)
    return 1.0


def venue_mult(p, dex):
    """Auf der Bonding-Kurve rutschen Stops oft weit durch (live im Schnitt −28 % statt −15 %) -> dort kleiner einsetzen."""
    from .agents.analysis import is_curve_dex
    return p.get("curve_size_mult", 1.0) if is_curve_dex(dex) else 1.0


def swing_pct(prices):
    """Schwankung der letzten 30 min: Spanne zwischen Hoch und Tief in % vom Mittel."""
    prices = [x for x in prices if x and x > 0]
    if len(prices) < 3:
        return None
    avg = sum(prices) / len(prices)
    return (max(prices) - min(prices)) / avg * 100


def stop_for(p, swing):
    """Stop-Abstand in %: fest – oder, wenn vol_stop an ist, nach der Schwankung (zwischen 6 und 35 %)."""
    if p.get("vol_stop") and swing:
        return round(max(6.0, min(35.0, swing * p.get("vol_mult", 0.6))), 1)
    return float(p["stop_pct"])


def exit_decision(p, st, price, reserve, buys, sells, now):
    """Prüft alle Ausstiegsregeln in fester Reihenfolge.

    st: market_entry, peak_price, opened_at, tp1_done, entry_liq, stop_pct (oder None), flow_since (oder None/0)
    -> (Aktion, Anteil, Grund, neues flow_since)   Aktion: None | 'exit' | 'partial'
    """
    ent = st["market_entry"]
    peak = max(st.get("peak_price") or 0, price)
    gain = (price / ent - 1) * 100
    peak_gain = (peak / ent - 1) * 100
    held = (now - st["opened_at"]) / 60
    stop = st.get("stop_pct") or p["stop_pct"]
    flow_since = st.get("flow_since") or 0
    if st.get("entry_liq") and reserve < st["entry_liq"] * 0.3:
        return "exit", 1.0, f"Rug-Alarm: Liquidität von {st['entry_liq']:,.0f} $ auf {reserve:,.0f} $ eingebrochen", flow_since
    if gain <= -stop:
        return "exit", 1.0, f"Stop-Loss bei {gain:+.1f} % (Grenze −{stop:g} %)", flow_since
    if not st.get("tp1_done") and gain >= p["tp1_pct"]:
        return "partial", p["tp1_sell_pct"] / 100, f"Teilverkauf {p['tp1_sell_pct']} % bei {gain:+.0f} %", flow_since
    if peak_gain >= p["trail_start_pct"] and price <= peak * (1 - p["trail_pct"] / 100):
        return "exit", 1.0, f"Trailing-Stop: {p['trail_pct']} % unter Hoch (+{peak_gain:.0f} %), jetzt {gain:+.0f} %", flow_since
    # Kaufdruck kippt: mehr Verkäufe als Käufe (Faktor flow_ratio) – mindestens 30 s am Stück, und nur solange kein großer Gewinn läuft
    if p.get("flow_exit"):
        bad = sells > max(1, buys) * p.get("flow_ratio", 1.3) and peak_gain < p["trail_start_pct"]
        if bad:
            flow_since = flow_since or now
            if now - flow_since >= 30:
                return "exit", 1.0, f"Kaufdruck gekippt: {sells} Verkäufe / {buys} Käufe in 5 min ({gain:+.0f} %)", flow_since
        else:
            flow_since = 0
    if p.get("time_stop_min") and held >= p["time_stop_min"] and peak_gain < p.get("time_stop_gain_pct", 5):
        return "exit", 1.0, f"Zeit-Stop: nach {p['time_stop_min']} min nie mehr als +{p.get('time_stop_gain_pct', 5)} % ({gain:+.0f} %)", flow_since
    if held >= p["max_hold_min"]:
        return "exit", 1.0, f"Höchst-Haltedauer {p['max_hold_min']} min erreicht ({gain:+.0f} %)", flow_since
    return None, 0.0, "", flow_since
