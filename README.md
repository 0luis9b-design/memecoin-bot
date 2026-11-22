# Memecoin-Bot 🐸 (nur Spielgeld)

Ein Paper-Trading-Bot für frische Solana-Memecoins, gebaut nach dem Vorbild der „Grok Bot“-TikToks:
mehrere kleine Agenten mit je einer Aufgabe, ein Dashboard und Telegram-Meldungen.
**Es gibt keine Wallet, keinen privaten Schlüssel und keinen echten Kauf.**

## Zwei getrennte Strategien

| Konto | Woher kommt das Signal? |
|---|---|
| **Scanner** | eigene Suche: neue/heiße Pools mit viel Volumen, Kaufdruck und Momentum |
| **Copy-Trading** | „Smart Wallets“, die der Bot selbst gefunden hat, kaufen einen Coin |

Beide starten mit 1.000 $ Spielgeld. Dazu kommen Testkonten mit abgewandelten Regeln (z. B. „Vorsichtig“, „Locker“,
„Schnell“ und „TikTok“ = Stop −6 %, Trailing ab +12 %, 8 % Einsatz wie in den Videos).

## Die Agenten

1. **Scanner**: GeckoTerminal (neue Pools, Trending 5 min) und DexScreener (neue Profile/Boosts). Er hält die Kurse
   aller beobachteten Coins aktuell: heiße Coins und offene Positionen alle 20 s, der Rest alle 2 min.
2. **Rug-Check** (RugCheck.xyz) prüft harte Regeln:
   - Mint- und Freeze-Funktion aus
   - keine Transfergebühr
   - Top 10 Holder ≤ 30 % (Pools und Locker zählen nicht)
   - mind. 50 Holder
   - Ersteller ≤ 5 %
   - Liquidität gesperrt
   - keine Insider-Netzwerke und keine Gefahr-Warnung
3. **Analyst**: Punktzahl 0–100 aus Momentum, Kaufdruck, Volumen-Tempo, Aktivität, Liquidität, Sicherheit und Chart.
4. **Sizer**: Einsatz 4–10 % vom Konto je nach Punktzahl. Der eigene Kauf darf den Kurs höchstens 2,5 % bewegen,
   höchstens 6 Positionen sind offen und höchstens 60 % des Kontos im Markt.
5. **Executor** rechnet ehrlich:
   - Die Order wird frühestens nach 15 s zum **dann** gültigen Kurs ausgeführt.
   - Kosten: 1 % Gebühr, 0,15 $ Netzwerk und 0,5 % Sandwich-Zuschlag, dazu die Kursbewegung im Pool (Konstantprodukt).
   - Bei der Pump.fun-Kurve wird mit den virtuellen Reserven gerechnet.
   - Ist der Kurs bis zur Ausführung um mehr als 25 % gestiegen, wird die Order verworfen.
6. **Risk-Guard**:
   - Stop-Loss −15 %
   - Teilverkauf 50 % bei +100 %
   - Trailing-Stop ab +25 % mit 15 % Abstand
   - max. 2 h Haltedauer
   - Rug-Alarm, wenn die Liquidität um 70 % einbricht
   - ein verschwundener Pool gilt nach 15 min als wertlos
7. **Logger**: Jede Entscheidung landet mit Begründung im Log, auch Ablehnungen.
8. **Wallet-Scout** sammelt die Trades junger Coins und bewertet jede Wallet: Wie oft lag sie mit ihren Käufen ≥ 1,5x
   im Plus? Bots, Kleinstbeträge und Wallets, die überall dabei sind, fliegen raus. Die besten 60 werden beobachtet.
9. **Copy-Feed** fragt die beobachteten Wallets über den Solana-RPC ab. Kauft eine, prüft der Bot:
   - Aufpreis höchstens 15 % gegenüber dem Einstieg der Wallet
   - derselbe Rug-Check wie beim Scanner
   - Punktzahl aus Analyst, Wallet-Score und der Zahl der Wallets, die denselben Coin kaufen
   
   Verkauft die Wallet, verkauft der Bot mit.
10. **Lern-Agent** (alle 6 h): Ist ein Testkonto nach mindestens 30 Trades mindestens 10 %-Punkte besser, mit besserem
    Profit-Faktor und ohne viel größeren Drawdown, entsteht ein **Vorschlag**. Das Hauptkonto ändert sich erst nach
    Freigabe im Dashboard. Ein klar schlechtes Testkonto wird durch eine Abwandlung des besten Kontos ersetzt.

## Neu in v1.1

- **Launch-Feed**: Neue Pump.fun-Coins kommen in Echtzeit über den kostenlosen PumpPortal-Websocket, zusammen mit
  den Migrationen zu PumpSwap. Nach 2–15 min prüft der Bot per DexScreener, ob ein Coin lebt. Nur dann kommt er auf
  die Beobachtungsliste. Der Rug-Check erkennt damit auch **Serien-Launcher**: Hat eine Ersteller-Wallet in 24 h
  mehr als 5 Coins gestartet, wird der Coin blockiert. Trades einzelner Coins kosten bei PumpPortal Gebühren und
  werden deshalb nicht genutzt.
- **Schnelle Spur**: Offene Positionen werden alle 5 s statt alle 20 s neu bewertet. Stops greifen dadurch früher.
  Verschwindet der Pool einer Position, sucht der Bot automatisch den neuen Pool, zum Beispiel nach einer Migration.
- **Schatten-Auswertung** (Tab „Auswertung“): Jede Ablehnung des Hauptkontos wird 2 h weiterverfolgt. Je Regel steht
  dort, wie viele der abgelehnten Coins Gewinner (≥ +50 %) oder Rugs waren, im Vergleich zu den gekauften Coins.
  Wird ein Testkonto ersetzt, lockert der Lern-Agent gezielt die Regel, die am deutlichsten zu streng wirkt.
- **Gewichte des Analysten**: Ab 100 Beispielen rechnet der Lern-Agent aus, welche Bestandteile Gewinner von
  Verlierern unterscheiden. Die neuen Gewichte testet er im Testkonto „Scanner · Gewichte“. Ins Hauptkonto kommen
  sie nur nach deiner Freigabe.

## Railway

| Variable | Zweck |
|---|---|
| `DATA_DIR` | Ordner im Volume, z. B. `/data` (sonst geht beim Neustart alles verloren) |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | Login fürs Dashboard |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram-Meldungen (optional) |
| `SOLANA_RPC_URL` | optional: eigener kostenloser RPC (z. B. Helius Free), falls der öffentliche Zugang bremst |

Alle Zahlen aus `memebot/config.py` lassen sich ebenfalls per Variable ändern (z. B. `START_BALANCE`, `FEE_PCT`).

## Lokal

```
pip install -r requirements-dev.txt
AUTOSTART=0 python -m pytest -q
python app.py
```

Alle Datenquellen sind kostenlos und brauchen keinen Schlüssel.
