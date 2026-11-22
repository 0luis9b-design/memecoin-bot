"""Launch-Feed: neue Pump.fun-Coins in Echtzeit über den kostenlosen PumpPortal-Websocket.

Kostenlos sind nur „neue Coins“ und „Migrationen“ (Wechsel von der Pump.fun-Kurve zu PumpSwap).
Trades einzelner Coins oder Wallets kosten bei PumpPortal Gebühren und brauchen eine Wallet – die nutzen wir nicht.

Ablauf:
  1. Jeder neue Coin landet sofort in der Tabelle „launches“ (mit Ersteller-Wallet und Erstkauf).
  2. Nach 2–15 min prüft der Bot per DexScreener, ob der Coin lebt (Volumen, Käufe, Marktkap.).
     Nur dann kommt er auf die Beobachtungsliste des Scanners.
  3. Der Rug-Check zählt, wie viele Coins dieselbe Ersteller-Wallet in 24 h gestartet hat (Serien-Launcher).
  4. Bei einer Migration sucht der Bot den neuen Pool, damit Kurse und Positionen weiterlaufen.
"""
import json
import logging
import threading
import time
from collections import deque

from .. import config, market
from ..sources import SourceError

log = logging.getLogger("memebot.launches")
WS_URL = "wss://pumpportal.fun/api/data"


def parse_message(raw):
    """-> ('create', dict) | ('migrate', mint) | (None, None)"""
    try:
        m = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(m, dict):
        return None, None
    tx = (m.get("txType") or "").lower()
    if tx == "create" and m.get("mint"):
        return "create", {
            "mint": m["mint"], "curve": m.get("bondingCurveKey") or "", "creator": m.get("traderPublicKey") or "",
            "symbol": str(m.get("symbol") or "?")[:24], "name": str(m.get("name") or "")[:60],
            "initial_sol": market.f(m.get("solAmount")), "mcap_sol": market.f(m.get("marketCapSol")),
        }
    if (tx in ("migrate", "migration") or "migrat" in tx) and m.get("mint"):
        return "migrate", m["mint"]
    return None, None


class LaunchFeed:
    name = "launchfeed"

    def __init__(self, store):
        self.s = store
        self.migrations = deque(maxlen=500)
        self.stats = {"verbunden": False, "neue_coins": 0, "migrationen": 0, "aufgenommen": 0, "verworfen": 0,
                      "neustarts": 0, "letzte_nachricht": None, "fehler": "", "pro_min": 0.0}
        self._recent = deque(maxlen=2000)
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ Websocket
    def start(self):
        if self._thread or not config.PUMPPORTAL_ENABLED:
            return
        self._thread = threading.Thread(target=self._run, name="launchfeed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            import websocket  # websocket-client
        except ImportError:
            self.stats["fehler"] = "Paket websocket-client fehlt"
            return
        backoff = 10
        while not self._stop.is_set():
            started = time.time()
            try:
                ws = websocket.create_connection(WS_URL, timeout=30)
                ws.send(json.dumps({"method": "subscribeNewToken"}))
                ws.send(json.dumps({"method": "subscribeMigration"}))
                self.stats["verbunden"] = True
                self.stats["fehler"] = ""
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping()
                        continue
                    if not raw:
                        break
                    self.handle(raw)
                ws.close()
            except Exception as e:  # noqa: BLE001 – Verbindung darf nie den Bot stören
                self.stats["fehler"] = f"{type(e).__name__}: {str(e)[:120]}"
            self.stats["verbunden"] = False
            self.stats["neustarts"] += 1
            # PumpPortal sperrt Clients, die ständig neu verbinden -> lange warten, wenn es nicht lange lief
            backoff = 10 if time.time() - started > 300 else min(300, backoff * 2)
            self._stop.wait(backoff)

    def handle(self, raw):
        kind, data = parse_message(raw)
        now = time.time()
        self.stats["letzte_nachricht"] = now
        if kind == "create":
            self.s.x("""INSERT OR IGNORE INTO launches(mint,curve,creator,ts,symbol,name,initial_sol,mcap_sol)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (data["mint"], data["curve"], data["creator"], now, data["symbol"], data["name"],
                      data["initial_sol"], data["mcap_sol"]))
            self.stats["neue_coins"] += 1
            self._recent.append(now)
            self.stats["pro_min"] = round(sum(1 for t in self._recent if now - t < 60), 1)
        elif kind == "migrate":
            self.migrations.append(data)
            self.stats["migrationen"] += 1

    # ------------------------------------------------------------ Nachprüfung (läuft im Takt)
    def check(self, scanner, max_calls=3):
        """Frische Coins nach 2–15 min per DexScreener prüfen; lebendige kommen auf die Beobachtungsliste."""
        now = time.time()
        self.s.x("UPDATE launches SET status='verworfen' WHERE status='neu' AND ts<?", (now - config.LAUNCH_CHECK_MAX_MIN * 60,))
        rows = self.s.q("""SELECT * FROM launches WHERE status='neu' AND ts<? AND (checked_at IS NULL OR checked_at<?)
                           ORDER BY ts LIMIT ?""", (now - config.LAUNCH_CHECK_MIN_SEC, now - 90, max_calls * 30))
        if not rows:
            return 0
        try:
            data = market.pairs([r["curve"] for r in rows if r["curve"]])
        except SourceError as e:
            self.stats["fehler"] = str(e)
            return 0
        added = 0
        for r in rows:
            d = data.get(r["curve"])
            alive = d and (d["vol_m5"] >= config.LAUNCH_MIN_VOL_M5 or d["buys_m5"] >= config.LAUNCH_MIN_BUYS_M5
                           or d["mcap"] >= config.LAUNCH_MIN_MCAP)
            if alive:
                d["created_at"] = d.get("created_at") or r["ts"]
                scanner._add(d, "pumpportal")
                self.s.x("UPDATE launches SET status='aufgenommen', checks=checks+1, checked_at=? WHERE mint=?", (now, r["mint"]))
                self.stats["aufgenommen"] += 1
                added += 1
            elif r["checks"] + 1 >= 3:
                self.s.x("UPDATE launches SET status='verworfen', checks=checks+1, checked_at=? WHERE mint=?", (now, r["mint"]))
                self.stats["verworfen"] += 1
            else:
                self.s.x("UPDATE launches SET checks=checks+1, checked_at=? WHERE mint=?", (now, r["mint"]))
        return added

    def handle_migrations(self, scanner):
        """Nach einer Migration den neuen (größeren) Pool übernehmen – nur für Coins, die wir beobachten."""
        mints = []
        while self.migrations:
            mints.append(self.migrations.popleft())
        if not mints:
            return 0
        known = [r["mint"] for r in self.s.q("SELECT mint FROM tokens WHERE mint IN (%s)" % ",".join("?" * len(mints)), mints)]
        if not known:
            return 0
        try:
            best = market.best_pair_for_mint(known)
        except SourceError:
            self.migrations.extend(known)   # später noch einmal versuchen
            return 0
        n = 0
        for mint, d in best.items():
            if d["dex"] not in ("pumpfun", "pump-fun"):
                self.s.x("UPDATE tokens SET pool=?, dex=?, status='watch', updated=0 WHERE mint=?", (d["pool"], d["dex"], mint))
                # offene Positionen ziehen mit um (gleicher Coin, neuer Pool)
                self.s.x("UPDATE positions SET pool=?, dex=? WHERE mint=? AND status IN ('pending','open')", (d["pool"], d["dex"], mint))
                self.s.decide("scanner", "INFO", f"Migration: {d['symbol']} handelt jetzt auf {d['dex']}", mint=mint, symbol=d["symbol"])
                n += 1
        return n
