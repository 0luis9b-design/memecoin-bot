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

## Neu in v1.2

- **Replay-Backtest** (Tab „Backtest“, automatisch alle 12 h oder per Knopf): Spielt die gespeicherten Kurs-Aufnahmen
  der letzten 72 h mit über 100 Regelsätzen erneut ab, mit denselben Regeln, Gebühren und Verzögerungen wie live
  (`memebot/rules.py`). Ausgewählt wird auf den ersten 70 % der Zeit, geprüft auf den letzten 30 %. Der beste
  robuste Regelsatz wird ein Testkonto „Scanner · Replay“; ins Hauptkonto kommt er nur nach Freigabe.
  Kurs-Aufnahmen werden jetzt 4 Tage aufbewahrt.
- **Neue Ausstiege** als Regeln und Testkonten:
  - Zeit-Stop: nach 10 min kein Plus → raus
  - Kaufdruck-Ausstieg: 30 s lang deutlich mehr Verkäufe als Käufe
  - Stop nach Schwankung: zwischen 6 und 35 %
- **Copy-Trading mit Rückmeldung**: Der Wallet-Score richtet sich auch danach, wie *unsere* Kopien gelaufen sind.
  Ab 5 Kopien mit Verlust und weniger als 30 % Treffern folgt der Bot der Wallet nicht mehr. Testkonto „Cluster“:
  Kauf erst, wenn mindestens 2 Smart Wallets kaufen.
- **Marktlage**: Die Markt-Hitze von 0–100 ergibt sich aus neuen Coins pro Minute, dem Anteil der Coins im Plus, den
  heißen Coins und dem SOL-Kurs. Das Testkonto „Marktlage“ setzt bei kaltem Markt die Hälfte ein und pausiert unter 15.
  Die Auswertung nach Uhrzeit steht im Tab „Auswertung“.
- **Neue Merkmale ohne Gewicht**: DexScreener-Boost und Holder-Wachstum. Der Lern-Agent schaltet sie ein, wenn sie
  Gewinner erkennen.

## Neu in v1.3 (nach 22 h Live-Daten)

**Befund:**
- **Hinterherrennen verliert:** Nach großen 5-min-Anstiegen zu kaufen hat im Hauptkonto −55 % gebracht, bei 31 % Treffern und Profit-Faktor 0,59. Der Backtest kommt auf ähnliche Werte.
- **Stops rutschen durch:** Sie greifen im Schnitt bei −28 % statt −15 %, weil die Kurse in Sekunden abstürzen.
- **Viele Rugs:** 40 % der gekauften Coins waren innerhalb von 2 h Rugs.
- **Die Punktzahl hilft nicht:** Sie unterscheidet Gewinner nicht von Verlierern.

**Änderungen:**
- **Neue Strategie-Familien als Testkonten:**
  - Kein Hinterherrennen
  - Rücksetzer: 1 h stark gestiegen, gerade 0 bis −20 % in 5 min
  - Nur migriert: keine Bonding-Kurve
  - Groß & alt: ab 30.000 $ Liquidität, mind. 1 h alt
  - Kombinationen daraus
- **Aufgeräumt:** Die 7 Verlierer-Varianten von „nach Anstieg kaufen“ sind abgeschaltet.
- **Datenfehler behoben:**
  - Unplausible Kurssprünge nach oben (über ×10) werden erst nach 3 Bestätigungen übernommen.
  - Die Auswertung zeigt Mediane statt Durchschnitte.
  - Der Backtest nutzt den Zeitraum, für den es vollständige Aufnahmen gibt.

## Neu in v1.4

- **Rug-Wächter**: Für offene Positionen lädt der Bot den RugCheck-Bericht alle 2,5 min neu. Er verkauft sofort, wenn
  - der Ersteller die Hälfte seiner Coins abstößt,
  - ein großer Holder (ab 3 %) die Hälfte verkauft,
  - die Holder um mehr als 10 % fallen,
  - die Liquidität entsperrt wird
  - oder eine neue Gefahr-Warnung kommt.

  Die Schatten-Auswertung zeigt in der Zeile „Rug-Wächter-Verkauf“, was danach mit dem Kurs passiert ist.
- **Smart-Money-Bestätigung**: Neue Testkonten „Smart-Money bestätigt“ und „Rücksetzer + Smart-Money“ kaufen nur, wenn
  eine beobachtete Smart Wallet den Coin in den letzten 6 h gekauft hat, und geben dafür Bonuspunkte.
- **Sniper-Merkmal** (vorerst ohne Gewicht): Aus den gesammelten Start-Trades zählt der Bot, wie viele Wallets in den
  ersten 10 s gekauft haben.
- **Halber Einsatz auf der Bonding-Kurve**, weil Stops dort am weitesten durchrutschen.
- Rug-Wächter und halber Einsatz auf der Kurve gelten für alle Testkonten. Das Hauptkonto bekommt sie nur nach
  Freigabe über einen Lern-Vorschlag.
- Den Rug-Wächter und die Smart-Money-Regel kann der Backtest nicht nachspielen; dafür fehlen alte RugCheck-Berichte und
  eine unverfälschte Wallet-Liste.

## Neu in v1.5 (24.11.2026)

- **Neue Runde:** Das Hauptkonto Scanner hatte rund −80 % verloren, weil es nach Anstiegen gekauft hat. Es ist jetzt als
  „Scanner · Runde 1 (archiviert)“ gespeichert. Das neue Hauptkonto startet mit 1.000 $ und den Regeln von
  „Migriert + Rücksetzer“, inklusive Rug-Wächter.
- **Abgeschaltet:** „Vorsichtig“ und „Gewichte G1“ (−70 und −66 %).
- **Backtest mit Fokus:** Er wandelt nur noch die Rücksetzer-Familie ab. Bis zu 3 robuste Regelsätze je Lauf werden
  Testkonten, höchstens 5 Replay-Konten laufen gleichzeitig. Wenn kein Platz frei ist, wird das schwächste ältere ersetzt.

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

### v1.5.1 (25.11.2026)
- **Fehler behoben: Phantom-Gewinne durch Kursfehler.** Fiel der Kurs zwischen Signal und Ausführung wegen eines Datenfehlers auf fast null, kaufte der Bot zum Fantasiekurs (Slippage 8.939 %) und buchte danach +1.364 % („Jensen“, +1.219 $ in jedem Scanner-Konto).
- Neu: Kauf wird verworfen, wenn die Slippage über 30 % liegt (so ein Swap würde in echt scheitern). Fällt der Kurs vor der Ausführung um mehr als 50 %, wartet die Order auf einen frischen Kurs.
- Gleiche Sperre im Backtest.
- Bereits gebuchte Phantom-Trades werden einmalig als „ungültig“ markiert und aus Kontostand und Equity-Kurve herausgerechnet.
- Logs gehen nach stdout (Railway zeigt sie nicht mehr als „error“).

