"""Alle Einstellungen an einem Ort. Jede Zahl kann auf Railway per Variable überschrieben werden.

Nur Spielgeld: Es gibt keine Wallet, keinen privaten Schlüssel und keinen echten Kauf.
"""
import os


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _i(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except ValueError:
        return int(default)


def _b(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "ja", "yes", "on")


DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
AUTOSTART = _b("AUTOSTART", True)
HTTP_TIMEOUT = _f("HTTP_TIMEOUT", 12)

# --- Konten ---
START_BALANCE = _f("START_BALANCE", 1000)       # $ Spielgeld je Konto (Hauptkonto und jedes Testkonto)

# --- Takt (Sekunden) ---
TICK_SEC = _f("TICK_SEC", 5)                    # Grundtakt der Schleife
SCAN_SEC = _f("SCAN_SEC", 60)                   # neue Pools suchen
REFRESH_OPEN_SEC = _f("REFRESH_OPEN_SEC", 5)    # Kurse für offene Positionen und Orders (schnelle Spur)
REFRESH_HOT_SEC = _f("REFRESH_HOT_SEC", 20)     # Kurse für heiße Coins
REFRESH_ALL_SEC = _f("REFRESH_ALL_SEC", 120)    # Kurse für die übrige Beobachtungsliste
HARVEST_SEC = _f("HARVEST_SEC", 60)             # Trades frischer Coins einsammeln (Wallet-Suche)
WALLET_SCORE_SEC = _f("WALLET_SCORE_SEC", 600)  # Smart Wallets neu bewerten
COPY_SEC = _f("COPY_SEC", 30)                   # beobachtete Wallets abfragen
LEARN_SEC = _f("LEARN_SEC", 6 * 3600)           # Lern-Agent
EQUITY_SEC = _f("EQUITY_SEC", 300)              # Kontostand-Punkt für die Kurve

# --- Beobachtungsliste ---
WATCH_MAX = _i("WATCH_MAX", 400)                # höchstens so viele Coins gleichzeitig beobachten
WATCH_MAX_AGE_H = _f("WATCH_MAX_AGE_H", 24)     # ältere Pools fliegen raus (außer offene Position)
NEW_POOL_MIN_RESERVE = _f("NEW_POOL_MIN_RESERVE", 2500)   # $ Reserve, damit ein neuer Pool überhaupt beobachtet wird
DEAD_LIQ = _f("DEAD_LIQ", 1500)                 # darunter gilt ein Coin als tot
DEAD_AFTER_MIN = _f("DEAD_AFTER_MIN", 30)       # … aber erst ab diesem Alter
QUIET_DEAD_MIN = _f("QUIET_DEAD_MIN", 60)       # ab diesem Alter fliegt ein Coin mit < 100 $ Volumen in 5 min raus
HOT_VOL_M5 = _f("HOT_VOL_M5", 2000)             # ab so viel $ Volumen in 5 min wird ein Coin oft aktualisiert
SNAPSHOT_KEEP_H = _f("SNAPSHOT_KEEP_H", 24)

# --- Simulation (ehrlich rechnen) ---
FEE_PCT = _f("FEE_PCT", 1.0)                    # Börsengebühr je Kauf/Verkauf in %
TX_COST_USD = _f("TX_COST_USD", 0.15)           # Netzwerk- und Prioritätsgebühr je Transaktion
EXTRA_SLIP_PCT = _f("EXTRA_SLIP_PCT", 0.5)      # Zuschlag für Sandwich-Bots und unruhige Kurse
FILL_DELAY_SEC = _f("FILL_DELAY_SEC", 15)       # Order wird frühestens so spät mit dem dann gültigen Kurs ausgeführt
MISSING_RUG_MIN = _f("MISSING_RUG_MIN", 15)     # Pool so lange verschwunden -> Position gilt als wertlos
SOL_FALLBACK_USD = _f("SOL_FALLBACK_USD", 140)

# --- Launch-Feed (PumpPortal, kostenlos: neue Coins + Migrationen) ---
PUMPPORTAL_ENABLED = _b("PUMPPORTAL_ENABLED", True)
LAUNCH_CHECK_MIN_SEC = _f("LAUNCH_CHECK_MIN_SEC", 120)   # frühestens so spät prüfen, ob der Coin lebt
LAUNCH_CHECK_MAX_MIN = _f("LAUNCH_CHECK_MAX_MIN", 15)    # danach gilt ein ruhiger Coin als verworfen
LAUNCH_MIN_VOL_M5 = _f("LAUNCH_MIN_VOL_M5", 1500)
LAUNCH_MIN_BUYS_M5 = _i("LAUNCH_MIN_BUYS_M5", 20)
LAUNCH_MIN_MCAP = _f("LAUNCH_MIN_MCAP", 12000)

# --- Schatten-Auswertung ---
SHADOW_H = _f("SHADOW_H", 2)                    # so lange werden abgelehnte Coins weiterverfolgt
SHADOW_MIN_N = _i("SHADOW_MIN_N", 30)           # ab so vielen ausgewerteten Coins gibt es eine Einschätzung
WEIGHT_MIN_SAMPLES = _i("WEIGHT_MIN_SAMPLES", 100)   # ab so vielen Beispielen lernt der Analyst neue Gewichte

# --- Quellen ---
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
RUGCHECK_PER_MIN = _i("RUGCHECK_PER_MIN", 20)
RUGCHECK_CACHE_MIN = _f("RUGCHECK_CACHE_MIN", 15)

# --- Copy-Trading ---
WALLETS_TRACK_MAX = _i("WALLETS_TRACK_MAX", 60)
WALLET_MIN_TOKENS = _i("WALLET_MIN_TOKENS", 3)
WALLET_MIN_SCORE = _f("WALLET_MIN_SCORE", 0.45)
WALLET_HIT_MULT = _f("WALLET_HIT_MULT", 1.5)    # Treffer = mind. 1,5x auf den Einstieg der Wallet
COPY_MAX_SIGNAL_AGE_SEC = _f("COPY_MAX_SIGNAL_AGE_SEC", 180)
COPY_WALLETS_PER_ROUND = _i("COPY_WALLETS_PER_ROUND", 30)

# --- Lernen ---
LEARN_MIN_TRADES = _i("LEARN_MIN_TRADES", 30)   # so viele abgeschlossene Trades braucht ein Vergleich mindestens
LEARN_MIN_EDGE_PCT = _f("LEARN_MIN_EDGE_PCT", 10)   # Testkonto muss um so viele % des Startkapitals besser sein

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_MIN_LEVEL = os.environ.get("NOTIFY_MIN_LEVEL", "normal")
NOTIFY_DAILY_CAP = _i("NOTIFY_DAILY_CAP", 40)
QUIET_FROM = _i("QUIET_FROM", 23)
QUIET_TO = _i("QUIET_TO", 7)
REPORT_HOUR = _i("REPORT_HOUR", 21)              # Tagesbericht (Berliner Zeit)
NOTIFY_PREFIX = os.environ.get("NOTIFY_PREFIX", "🐸 ")

WATCHDOG_MIN = _f("WATCHDOG_MIN", 10)
