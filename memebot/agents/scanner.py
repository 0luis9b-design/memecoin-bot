"""Scanner-Agent: findet neue und heiße Solana-Pools und hält ihre Kurse aktuell.

Quellen: GeckoTerminal (neue Pools, Trending 5 min) und DexScreener (neue Profile/Boosts).
Jeder beobachtete Coin bekommt regelmäßig frische Werte von DexScreener; jeder Wert landet als Schnappschuss
in der Datenbank – daraus liest der Analyst den Chart.
"""
import json
import logging
import time

from .. import config, market
from ..sources import SourceError

log = logging.getLogger("memebot.scanner")


def ph(n):
    """Platzhalter für SQL-IN (leere Liste -> passt auf nichts)."""
    return ",".join("?" * n) if n else "NULL"


class Scanner:
    name = "scanner"

    def __init__(self, store):
        self.s = store
        self.stats = {"pools_gesehen": 0, "beobachtet": 0, "letzter_scan": None, "neu_letzter_scan": 0, "fehler": ""}
        self._page = 1
        self._trend_at = 0
        self._ds_at = 0

    # ------------------------------------------------------------ Entdecken
    def discover(self):
        found = []
        try:
            found += market.new_pools(self._page)
            self._page = 2 if self._page == 1 else 1   # abwechselnd Seite 1 und 2
        except SourceError as e:
            self.stats["fehler"] = str(e)
        now = time.time()
        if now - self._trend_at > 120:
            self._trend_at = now
            try:
                found += [dict(p, source="trending") for p in market.trending_pools()]
            except SourceError as e:
                self.stats["fehler"] = str(e)
        new = 0
        for p in found:
            self.stats["pools_gesehen"] += 1
            if p["reserve"] < config.NEW_POOL_MIN_RESERVE and p["vol_m5"] < config.HOT_VOL_M5:
                continue
            new += self._add(p, p.get("source", "neu"))
        if now - self._ds_at > 180:
            self._ds_at = now
            new += self._discover_dexscreener()
        self.stats["letzter_scan"] = now
        self.stats["neu_letzter_scan"] = new
        self.stats["beobachtet"] = self.s.val("SELECT COUNT(*) FROM tokens WHERE status='watch'", default=0)
        self._trim()
        return new

    def _discover_dexscreener(self):
        try:
            mints = market.ds_latest_lists()
            known = {r["mint"] for r in self.s.q("SELECT mint FROM tokens WHERE mint IN (%s)" % ph(len(mints)), mints)} if mints else set()
            fresh = [m for m in mints if m not in known][:60]
            best = market.best_pair_for_mint(fresh) if fresh else {}
        except SourceError as e:
            self.stats["fehler"] = str(e)
            return 0
        n = 0
        for d in best.values():
            if d["liq"] >= config.NEW_POOL_MIN_RESERVE or d["dex"] == "pumpfun":
                n += self._add(d, "dexscreener")
        return n

    def _add(self, p, source):
        if not p.get("mint") or not p.get("pool"):
            return 0
        row = self.s.one("SELECT pool, status FROM tokens WHERE mint=?", (p["mint"],))
        now = time.time()
        if row and row["status"] == "dead" and (source in ("trending", "dexscreener", "copy") or p.get("vol_m5", 0) >= config.HOT_VOL_M5):
            # totgeglaubter Coin ist wieder im Gespräch -> wieder beobachten
            self.s.x("UPDATE tokens SET status='watch', updated=0 WHERE mint=?", (p["mint"],))
            return 1
        if row:
            # Coin ist umgezogen (z. B. von der Pump.fun-Kurve zu PumpSwap): auf den größeren Pool wechseln
            if row["pool"] != p["pool"] and p.get("reserve", 0) > 0:
                cur = self.s.one("SELECT metrics FROM tokens WHERE mint=?", (p["mint"],))
                m = json.loads(cur["metrics"] or "{}") if cur else {}
                if p["reserve"] > m.get("reserve", 0) * 1.5:
                    self.s.x("UPDATE tokens SET pool=?, dex=?, status='watch' WHERE mint=?", (p["pool"], p["dex"], p["mint"]))
            return 0
        self.s.x("INSERT INTO tokens(mint,pool,symbol,name,dex,created_at,first_seen,source,status,first_price,max_price,"
                 "min_price,last_price,updated) VALUES(?,?,?,?,?,?,?,?,'watch',?,?,?,?,0)",
                 (p["mint"], p["pool"], p["symbol"], p.get("name", ""), p["dex"], p.get("created_at") or now, now, source,
                  p["price"], p["price"], p["price"], p["price"]))
        return 1

    def _trim(self):
        n = self.s.val("SELECT COUNT(*) FROM tokens WHERE status='watch'", default=0)
        if n > config.WATCH_MAX:
            # die ruhigsten Coins ohne offene Position fliegen zuerst raus
            self.s.x("""UPDATE tokens SET status='dead' WHERE mint IN (
                        SELECT mint FROM tokens WHERE status='watch' AND hot=0
                        AND mint NOT IN (SELECT mint FROM positions WHERE status IN ('pending','open'))
                        ORDER BY (first_seen > ?) ASC, COALESCE(json_extract(metrics, '$.vol_m5'), 0) ASC, first_seen ASC
                        LIMIT ?)""", (time.time() - 600, n - config.WATCH_MAX))

    # ------------------------------------------------------------ Aktualisieren
    def due_pools(self, hot_only):
        now = time.time()
        open_mints = {r["mint"] for r in self.s.q("SELECT mint FROM positions WHERE status IN ('pending','open')")}
        rows = self.s.q("SELECT mint, pool, hot, updated FROM tokens WHERE status='watch' OR mint IN (%s)"
                        % ph(len(open_mints)), list(open_mints))
        due = []
        for r in rows:
            age = now - (r["updated"] or 0)
            if r["mint"] in open_mints or r["hot"]:
                if age >= config.REFRESH_HOT_SEC - 2:
                    due.append((0 if r["mint"] in open_mints else 1, r["pool"]))
            elif not hot_only and age >= config.REFRESH_ALL_SEC:
                due.append((2, r["pool"]))
        due.sort()
        return [p for _, p in due]

    def refresh(self, hot_only=False, max_calls=8):
        """Holt frische Werte. Gibt {mint: Werte} der aktualisierten Coins zurück."""
        pools = self.due_pools(hot_only)[: max_calls * 30]
        if not pools:
            return {}
        try:
            data = market.pairs(pools)
        except SourceError as e:
            self.stats["fehler"] = str(e)
            return {}
        now = time.time()
        updated = {}
        snaps = []
        by_pool = {r["pool"]: r for r in self.s.q("SELECT * FROM tokens WHERE pool IN (%s)" % ph(len(pools)), pools)}
        for pool in pools:
            t = by_pool.get(pool)
            if not t:
                continue
            d = data.get(pool)
            if not d or d["price"] <= 0:
                # Pool fehlt in der Antwort: merken, seit wann (Rug-Verdacht bei offener Position)
                self.s.x("UPDATE tokens SET missing_since=COALESCE(missing_since, ?), updated=? WHERE mint=?", (now, now, t["mint"]))
                continue
            hot = 1 if d["vol_m5"] >= config.HOT_VOL_M5 else 0
            age_min = (now - (t["created_at"] or t["first_seen"])) / 60
            status = t["status"]
            if age_min > config.DEAD_AFTER_MIN and d["reserve"] * 2 < config.DEAD_LIQ and d["vol_m5"] < 200:
                status = "dead"
            if age_min > config.WATCH_MAX_AGE_H * 60 and not hot:
                status = "dead"
            if age_min > config.QUIET_DEAD_MIN and d["vol_m5"] < 100:
                status = "dead"
            self.s.x("""UPDATE tokens SET metrics=?, updated=?, last_price=?, max_price=MAX(COALESCE(max_price,0),?),
                        min_price=MIN(COALESCE(min_price,?),?), hot=?, status=?, missing_since=NULL,
                        symbol=COALESCE(NULLIF(?, '?'), symbol), name=COALESCE(NULLIF(?, ''), name),
                        first_price=COALESCE(first_price, ?) WHERE mint=?""",
                     (json.dumps(d), now, d["price"], d["price"], d["price"], d["price"], hot, status,
                      d["symbol"], d["name"], d["price"], t["mint"]))
            snaps.append((t["mint"], now, d["price"], d["mcap"], d["liq"] or d["reserve"] * 2, d["vol_m5"], d["buys_m5"], d["sells_m5"]))
            d["age_min"] = age_min
            d["source"] = t["source"]
            updated[t["mint"]] = d
        if snaps:
            self.s.many("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)", snaps)
        return updated

    def history(self, mint, minutes=30):
        return self.s.q("SELECT ts, price, vol_m5, buys_m5, sells_m5, liq FROM snapshots WHERE mint=? AND ts>? ORDER BY ts",
                        (mint, time.time() - minutes * 60))
