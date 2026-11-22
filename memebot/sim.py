"""Ehrliche Kauf-/Verkaufs-Simulation für Spielgeld.

Ein Pool wird als Konstantprodukt-Pool gerechnet (x * y = k) mit der SOL-Seite als Reserve in $:
  Kaufen  für X $  ->  Token = B * X' / (Q + X')      (X' = X nach Gebühr, B = Q / Kurs)
  Verkaufen T Token ->  $ = Q * T / (B + T) nach Gebühr
Dazu kommen Netzwerkgebühr und ein Zuschlag für Sandwich-Bots. Je kleiner der Pool, desto teurer der Trade –
und wenn bei einem Rug Pull die Liquidität verschwindet, bekommt man beim Verkauf fast nichts mehr.
"""
from . import config


def buy(usd, price, reserve_usd):
    """-> (Token, effektiver Preis, Gebühr $, Kursbewegung %)"""
    if usd <= 0 or price <= 0:
        return 0.0, 0.0, 0.0, 0.0
    fee = usd * config.FEE_PCT / 100 + config.TX_COST_USD
    x = max(0.0, usd - fee) * (1 - config.EXTRA_SLIP_PCT / 100)
    q = max(reserve_usd, 1.0)
    b = q / price
    tokens = b * x / (q + x)
    eff = usd / tokens if tokens else 0.0
    return tokens, eff, fee, impact_pct(x, q)


def sell(tokens, price, reserve_usd):
    """-> ($ Erlös, effektiver Preis, Gebühr $)"""
    if tokens <= 0 or price <= 0:
        return 0.0, 0.0, 0.0
    q = max(reserve_usd, 0.0)
    if q <= 0:
        return 0.0, 0.0, 0.0
    b = q / price
    gross = q * tokens / (b + tokens) * (1 - config.EXTRA_SLIP_PCT / 100)
    fee = gross * config.FEE_PCT / 100 + config.TX_COST_USD
    usd = max(0.0, gross - fee)
    return usd, (usd / tokens), fee


def impact_pct(usd, reserve_usd):
    """Um wie viel % würde unser Kauf den Kurs bewegen?"""
    q = max(reserve_usd, 1.0)
    return usd / q * 100 * (2 + usd / q)  # Kursanstieg im Konstantprodukt: (1 + x/q)^2 - 1


def max_usd_for_impact(max_pct, reserve_usd):
    """Größter Kauf, der den Kurs um höchstens max_pct % bewegt."""
    return max(0.0, reserve_usd * ((1 + max_pct / 100) ** 0.5 - 1))
